# Fill the Redmine fixture with content that exercises the importer.
#
# Run through Redmine's own models rather than SQL, so every row is written the
# way a real instance writes it: nested sets maintained, journals recorded by
# the same callbacks, attachments stored under whatever disk layout this
# version uses. A hand-built SQL seed would test the importer against our idea
# of Redmine rather than against Redmine.
#
#   make fixture-seed PROFILE=pg
#
# Idempotent: re-running adds nothing.
#
# Everything here is placeholder content. No real names, addresses or hosts.

require 'base64'

ADMIN_LOGIN = 'fixture_admin'.freeze

def say(message)
  puts "[seed] #{message}"
end

# Guards on the first thing created, not the last, so a run that died half way
# is still recognised. The fixture is disposable either way: reset it with
# `make fixture-reset`.
if User.exists?(login: ADMIN_LOGIN)
  say 'Already seeded; nothing to do.'
  exit 0
end

# Redmine 7 defaults to CommonMark. The fixture uses Textile so the importer's
# markup conversion is exercised; an instance that already stores Markdown is
# the easier case and is covered by unit tests.
Setting.text_formatting = 'textile'
# Off by default. Turned on so the fixture can hold a relation between issues in
# different projects, which is the case the importer's late relation phase
# exists for: the issue at the other end may be imported after the one naming it.
Setting.cross_project_issue_relations = '1'
Redmine::DefaultData::Loader.load('en') if Redmine::DefaultData::Loader.no_data?

# ---------------------------------------------------------------------------
# Principals: one account in each state Redmine supports, plus a group.
# ---------------------------------------------------------------------------

def make_user(login, first, last, status)
  user = User.new(login: login, firstname: first, lastname: last, language: 'en')
  user.password = 'fixture-password-not-a-secret'
  user.mail = "#{login}@example.invalid"
  user.status = status
  user.save!
  user
end

admin = make_user(ADMIN_LOGIN, 'Fixture', 'Admin', User::STATUS_ACTIVE)
admin.update!(admin: true)
developer = make_user('fixture_dev', 'Dev', 'Account', User::STATUS_ACTIVE)
locked = make_user('fixture_locked', 'Locked', 'Account', User::STATUS_LOCKED)
registered = make_user('fixture_new', 'Registered', 'Account', User::STATUS_REGISTERED)
# A non-Latin display name, to catch encoding assumptions.
thai = make_user('fixture_thai', 'ผู้ใช้', 'ทดสอบ', User::STATUS_ACTIVE)

# Asked by Redmine to change its password at next sign-in, which an import that
# keeps passwords has to carry over.
registered.update!(must_change_passwd: true)

# An account that signs in through a directory. Redmine never checks its local
# hash, so the importer must not carry one. The host is never contacted.
directory = AuthSourceLdap.create!(
  name: 'Fixture Directory',
  host: 'ldap.example.invalid',
  port: 389,
  base_dn: 'dc=example,dc=invalid',
  attr_login: 'uid'
)
ldap_user = User.new(login: 'fixture_ldap', firstname: 'Directory', lastname: 'Account', language: 'en')
ldap_user.auth_source = directory
ldap_user.mail = 'fixture_ldap@example.invalid'
ldap_user.save!

group = Group.create!(lastname: 'Platform Team')
group.users << developer
group.users << thai

User.current = admin

# ---------------------------------------------------------------------------
# Lookups: a custom status, so the importer meets one it cannot have seeded.
# ---------------------------------------------------------------------------

IssueStatus.find_or_create_by!(name: 'Awaiting Review') do |status|
  status.is_closed = false
  status.position = 10
end

# ---------------------------------------------------------------------------
# Custom fields: one per format the importer translates.
# ---------------------------------------------------------------------------

bug = Tracker.find_by_name('Bug')
feature = Tracker.find_by_name('Feature')

custom_fields = {
  'Severity' => { field_format: 'list', possible_values: %w[Low Medium High] },
  'Tags' => { field_format: 'list', possible_values: %w[ui backend infra], multiple: true },
  'Reviewer' => { field_format: 'user' },
  'Target Release' => { field_format: 'version' },
  'Story Points' => { field_format: 'int' },
  'Effort' => { field_format: 'float' },
  'Regression' => { field_format: 'bool' },
  'Found On' => { field_format: 'date' },
  'Reference' => { field_format: 'string' },
  'Notes' => { field_format: 'text' },
  'Spec Link' => { field_format: 'link' }
}

fields = custom_fields.each_with_object({}) do |(name, attrs), acc|
  acc[name] = IssueCustomField.create!(
    { name: name, is_for_all: true, trackers: [bug, feature] }.merge(attrs)
  )
end

# ---------------------------------------------------------------------------
# Projects: a parent, its subproject, and an unrelated one for scope tests.
# ---------------------------------------------------------------------------

parent = Project.create!(
  name: 'Acme App',
  identifier: 'acme-app',
  description: 'Main product. Uses "Textile":https://textile-lang.com formatting.',
  is_public: true
)
# Two of these have no Specivo equivalent and should be reported, not mapped.
parent.enabled_module_names = %w[issue_tracking wiki time_tracking repository boards]

child = Project.create!(name: 'Acme Mobile', identifier: 'acme-mobile', parent_id: parent.id)
child.enabled_module_names = %w[issue_tracking wiki]

archived = Project.create!(name: 'Acme Legacy', identifier: 'acme-legacy')
archived.enabled_module_names = %w[issue_tracking]
archived.update_column(:status, Project::STATUS_ARCHIVED)

# Created open so issues can target it, then locked, which is the state the
# importer should carry across.
version = Version.create!(
  project: parent,
  name: '1.0',
  description: 'First release',
  status: 'open',
  sharing: 'descendants',
  effective_date: Date.today + 30
)
category = IssueCategory.create!(project: parent, name: 'Backend', assigned_to: developer)

# ---------------------------------------------------------------------------
# Memberships: one direct, one through the group. Redmine records the group's
# grant on each member as an inherited row, which the importer must not
# double-count.
# ---------------------------------------------------------------------------

manager_role = Role.find_by_name('Manager')
developer_role = Role.find_by_name('Developer')

Member.create!(project: parent, user: admin, roles: [manager_role])
Member.create!(project: parent, principal: group, roles: [developer_role])
Member.create!(project: child, user: developer, roles: [developer_role])

# ---------------------------------------------------------------------------
# Access for people without a membership. The builtin Non member and
# Anonymous roles apply instance-wide; a membership held by the builtin
# GroupAnonymous / GroupNonMember principal overrides them on one project.
# login_required is left unset, Redmine's default; the importer tests set it.
# ---------------------------------------------------------------------------

# Anonymous visitors may read issues in the subproject but not its wiki.
issues_reader = Role.create!(name: 'Issue Reader', permissions: %i[view_issues view_gantt], assignable: true)
Member.create!(project: child, principal: GroupAnonymous.load_instance, roles: [issues_reader])
# Signed-in non-members get a reporter's rights on the parent, which Specivo
# has no per-project equivalent for.
Member.create!(project: parent, principal: GroupNonMember.load_instance, roles: [Role.find_by_name('Reporter')])

# A private project: never readable without a membership, whatever the roles say.
internal = Project.create!(name: 'Acme Internal', identifier: 'acme-internal', is_public: false)
internal.enabled_module_names = %w[issue_tracking wiki]

# ---------------------------------------------------------------------------
# Issues, including a parent with two children and non-Latin content.
# ---------------------------------------------------------------------------

parent_issue = Issue.create!(
  project: parent,
  tracker: bug,
  author: admin,
  priority: IssuePriority.default,
  status: IssueStatus.find_by_name('New'),
  category: category,
  fixed_version: version,
  subject: 'Login fails after session timeout',
  description: <<~TEXTILE,
    h2. Steps to reproduce

    # Sign in
    # Wait for the session to expire
    # Reload the page

    The error is in attachment:server.log and the flow is drawn in !diagram.png!

    See [[Architecture]] for background. Related to #2.

    <pre><code class="ruby">
    def refresh(token)
      token.renew!
    end
    </code></pre>
  TEXTILE
  start_date: Date.today - 20,
  due_date: Date.today + 10,
  estimated_hours: 7.5,
  done_ratio: 30,
  custom_field_values: {
    fields['Severity'].id => 'High',
    fields['Tags'].id => %w[ui backend],
    fields['Reviewer'].id => developer.id.to_s,
    fields['Target Release'].id => version.id.to_s,
    fields['Story Points'].id => '5',
    fields['Effort'].id => '2.25',
    fields['Regression'].id => '1',
    fields['Found On'].id => (Date.today - 20).to_s,
    fields['Reference'].id => 'REF-9001',
    fields['Notes'].id => "Multi-line\nnote text",
    fields['Spec Link'].id => 'https://example.invalid/spec'
  }
)

subtask_one = Issue.create!(
  project: parent,
  tracker: bug,
  author: developer,
  priority: IssuePriority.default,
  subject: 'Session cookie is not cleared',
  parent_issue_id: parent_issue.id
)

# A non-Latin subject and body.
Issue.create!(
  project: parent,
  tracker: feature,
  author: thai,
  priority: IssuePriority.default,
  subject: 'ปัญหาการเข้าสู่ระบบ',
  description: 'รายละเอียดของปัญหา'
)

# In the subproject, so a cross-project relation and a private issue exist.
mobile_issue = Issue.create!(
  project: child,
  tracker: bug,
  author: admin,
  priority: IssuePriority.default,
  subject: 'Crash on cold start',
  is_private: true
)

closed_issue = Issue.create!(
  project: parent,
  tracker: bug,
  author: admin,
  priority: IssuePriority.default,
  subject: 'Typo on the sign-in page',
  status: IssueStatus.find_by_name('New')
)

# ---------------------------------------------------------------------------
# History: an entry with only a note, one with only field changes, one of both.
# ---------------------------------------------------------------------------

# Reloaded first: creating the subtasks bumped the parent's lock version.
parent_issue.reload.init_journal(developer, 'Reproduced on staging. Looking into it.')
parent_issue.save!

parent_issue.reload.init_journal(admin)
parent_issue.status = IssueStatus.find_by_name('Awaiting Review')
parent_issue.done_ratio = 60
parent_issue.save!

parent_issue.reload.init_journal(admin, 'Fix is in review. See #3 for the follow-up.')
parent_issue.assigned_to = developer
parent_issue.save!

# A private note, which only some roles may read. Reloaded first: Redmine
# updates an issue's nested-set columns after creating it, which leaves the
# object we are holding a version behind.
private_journal = closed_issue.reload.init_journal(admin, 'Internal: raised by a customer.')
private_journal.private_notes = true
closed_issue.save!

closed_issue.reload.init_journal(admin)
closed_issue.status = IssueStatus.find_by_name('Closed')
closed_issue.save!

# ---------------------------------------------------------------------------
# Relations: one of each kind the importer maps, including a cross-project one.
# ---------------------------------------------------------------------------

IssueRelation.create!(issue_from: parent_issue, issue_to: closed_issue, relation_type: 'relates')
IssueRelation.create!(issue_from: closed_issue, issue_to: mobile_issue, relation_type: 'blocks')
IssueRelation.create!(issue_from: subtask_one, issue_to: mobile_issue, relation_type: 'precedes', delay: 2)
IssueRelation.create!(issue_from: parent_issue, issue_to: subtask_one, relation_type: 'duplicates') rescue nil

Watcher.create!(watchable: parent_issue, user: developer)
Watcher.create!(watchable: parent_issue, user: thai)

version.update!(status: 'locked')

# ---------------------------------------------------------------------------
# Wiki: a page with three revisions, a child page, and a rename leaving a
# redirect behind.
# ---------------------------------------------------------------------------

wiki = parent.wiki || Wiki.create!(project: parent, start_page: 'Wiki')

home = WikiPage.new(wiki: wiki, title: 'Architecture')
home.content = WikiContent.new(
  text: "h1. Architecture\n\nFirst draft. See #1 and [[Deployment]].",
  author: admin
)
home.save!

home.content.text = "h1. Architecture\n\nSecond draft with a *bold* point and attachment:notes.txt"
home.content.author = developer
home.content.comments = 'Expanded the overview'
home.content.save!

home.content.text = "h1. Architecture\n\nThird draft.\n\n|_. Component |_. Owner |\n| API | Platform |"
home.content.author = admin
home.content.comments = 'Added the component table'
home.content.save!

deployment = WikiPage.new(wiki: wiki, title: 'Deployment', parent: home)
deployment.content = WikiContent.new(text: 'Child page describing deployment.', author: developer)
deployment.save!

renamed = WikiPage.new(wiki: wiki, title: 'Old Runbook')
renamed.content = WikiContent.new(text: 'Runbook content.', author: admin)
renamed.save!
# Leaves a redirect behind, which the importer carries across.
renamed.title = 'Runbook'
renamed.save!

thai_page = WikiPage.new(wiki: wiki, title: 'คู่มือ')
thai_page.content = WikiContent.new(text: 'เนื้อหาภาษาไทย', author: thai)
thai_page.save!

# ---------------------------------------------------------------------------
# Attachments: one on an issue, one on a wiki page, one binary, and one of a
# type the target instance would not accept from an upload today.
# ---------------------------------------------------------------------------

# A 1x1 PNG, so a binary file is exercised without committing one.
PNG_BYTES = Base64.decode64(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=='
)

def attach(container, author, filename, bytes, description = nil)
  path = File.join('/tmp', filename)
  File.binwrite(path, bytes)
  attachment = Attachment.new(
    container: container,
    file: File.open(path, 'rb'),
    author: author,
    filename: filename,
    description: description
  )
  attachment.save!
  attachment
end

attach(parent_issue, admin, 'server.log', "session expired\nrenew failed\n", 'The failing session log')
attach(parent_issue, developer, 'diagram.png', PNG_BYTES, 'Session refresh flow')
attach(home, admin, 'notes.txt', "Architecture notes.\n")
# An executable is not in Specivo's upload allowlist; historical files still import.
attach(closed_issue, admin, 'legacy-tool.exe', "MZ\x00\x00binary placeholder".b)

# ---------------------------------------------------------------------------
# Time entries, including one with more precision than two decimal places.
# ---------------------------------------------------------------------------

development = TimeEntryActivity.find_by_name('Development') || TimeEntryActivity.first
design = TimeEntryActivity.find_by_name('Design') || development

TimeEntry.create!(
  project: parent, issue: parent_issue, user: developer, author: developer,
  activity: development, hours: 2.5, spent_on: Date.today - 3, comments: 'Investigating'
)
TimeEntry.create!(
  project: parent, issue: parent_issue, user: admin, author: developer,
  activity: design, hours: 1.333333, spent_on: Date.today - 2, comments: 'Reviewing the fix'
)
# Time logged against the project rather than an issue.
TimeEntry.create!(
  project: child, user: developer, author: developer,
  activity: development, hours: 0.75, spent_on: Date.today - 1
)

say "projects=#{Project.count} issues=#{Issue.count} journals=#{Journal.count} " \
    "wiki_pages=#{WikiPage.count} versions=#{WikiContentVersion.count} " \
    "attachments=#{Attachment.count} time_entries=#{TimeEntry.count} " \
    "users=#{User.where(type: 'User').count} groups=#{Group.count} relations=#{IssueRelation.count}"
say 'Seeded.'

"""Instructions for turning a messy job-application ledger into rows."""

IMPORT_PROMPT = """\
You read a person's own notes about job applications and turn them into rows
for their hiring pipeline. The notes have no fixed format: lists, sentences,
fragments, mixed languages.

INPUT
The PROJECT_DATA block holds "today" (an ISO date) and "ledger": the notes, one
numbered line per line, as "<number>: <text>". Placeholders such as [LINK_1],
[EMAIL_2], [PHONE_1] and [SALARY_1] stand for removed details. Copy them exactly
when they belong in a note; never invent new ones.

ROWS
One row per application (one company, or one company and role). For each row:
- lines: every line number the row was built from.
- name: the company. Required. Keep the person's spelling.
- role: the job title, or null.
- status: exactly one of Contacted, Applied, Interviewing, Offer, Rejected.
  Contacted = talked to someone but did not apply. Interviewing = any interview
  round is booked or done. Offer and Rejected are final outcomes.
- date: when they applied or first made contact, or null.
- stage_date: the date the CURRENT status was reached, only when the notes say
  so ("interview on Sep 20" for Interviewing). Otherwise null. Do not reuse the
  application date.
- next_action: the next thing they plan to do, short, or null.
- next_action_date: when, or null.
- note: anything else worth keeping, short, or null.
- must_have_techs: the required technologies the lines name for this job
  (languages, frameworks, databases, cloud, tools), most important first, at
  most 5. Use each tech's common name ("React", "PostgreSQL"). Include techs
  named anywhere in the lines, the job title too ("Senior React Engineer" ->
  React). Never guess a stack the text does not name. Empty list if none.

DATES
Return ISO dates (YYYY-MM-DD). Resolve relative dates ("next Tuesday",
"yesterday") against today.
Short dates like "02-07" or "15/6" are day and month in one order for the whole
ledger: work the order out from any value over 12 ("15-06" means day-month).
If the year is missing, use the most recent year that keeps the date on or
before today. If a date still cannot be resolved with confidence, return null.
Never guess a date.

UNPLACED
List the numbers of lines that mention no application (headings, unrelated
thoughts) in "unplaced". Every line is either in some row's lines or unplaced.
"""

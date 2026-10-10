# Dossier questionnaire

The CV is a thin signal: it says what you did, not how big it was, what you actually enjoyed, or
what you would turn down. The **dossier** is the AI-only addendum to the profile (`dossier:` in
`config/profile.yaml`) that fills that gap. It is fed only to the Opus ranking prompt and the
refine pass, never to the Sonnet screen, so it can be as long as it needs to be.

How to use this file:

1. Answer in prose, in whatever language you think in. Skip anything that does not apply; one
   sentence is fine, three is plenty. Honest beats flattering: "I ran the Docker setup once, from
   a tutorial" ranks you better than "Docker", because the ranker stops guessing.
2. Put the answers in `config/dossier-answers.md` (gitignored, mirrored into the private repo by
   `scripts/sync_private.sh`) or paste them into the chat.
3. The answers get condensed into the `dossier:` block of the profile, and section 9 becomes the
   `weights:` rubric. Both are optional; leaving them out keeps the prompts unchanged.

The questions are numbered so an answer can refer to one ("see 2.3").

## 1. Projects — the real size and your real role

1.1 Name your three or four most important projects. For each: how long it took, how many
people worked on it, roughly how big it is (files, lines, services, users — whatever you know),
and whether it is public.

1.2 For each of those: what did *you* build alone, start to finish? What did someone else do?

1.3 Which project would you demo in an interview, and what would you show first?

1.4 Which part of any project are you least proud of, and why? (A ranker that knows the weak spot
can flag jobs that would hit it.)

1.5 What have you built outside courses — anything, however small, that you made because you
wanted it to exist?

## 2. Tools — by actual proficiency, not by exposure

2.1 Sort your tools into four bands: **daily / fluent** (you could start on Monday), **used in a
real project** (you would need a day to get back in), **tried** (a tutorial, a weekend), **name
only**. Languages, frameworks, databases, infrastructure, editors, everything.

2.2 Which language do you want to be hired for, and which ones are you willing to be hired for?

2.3 Is there a stack you would rather not work in at all? (PHP, Java enterprise, .NET, mobile,
anything.)

2.4 Testing, CI/CD, containers, cloud: what have you actually done, not read about? One line per
area.

2.5 What are you learning right now, and what would you learn next if a job asked for it?

## 3. Domains and kind of work

3.1 Which problem domains excite you? (Infrastructure, games, embedded and hardware, finance,
health, data pipelines, developer tools, web products, AI tooling, public sector…)

3.2 Which domains would you take only if nothing else came up, and which never?

3.3 Product company, internal IT, agency, consultancy placed at clients, research group, public
sector: rank these, and say which you would refuse.

3.4 How much do you want to be close to the hardware or the operating system versus close to the
user? Does "full-stack web" sound like the goal or like the fallback?

3.5 Would you take a role that is mostly maintenance of an existing system, if the stack and
the team were right?

## 4. Level and title

4.1 In your own words: what does "junior" mean for you, and what would a job have to ask for
before you consider yourself under-qualified?

4.2 Would you apply to a mid-level title if the stack fits exactly? Where is the line?

4.3 How many years of experience would you claim, counting what? (Project-based study, freelance,
jobs, anything.)

4.4 Trainee, internship, working-student positions: still interesting, or only if paid above a
certain level? Unpaid internships?

4.5 Have you been told "over-qualified" or "under-qualified" so far, and for what?

## 5. Conditions and deal-breakers

5.1 On-call duty: never, occasionally with compensation, or fine?

5.2 Travel: what share of time is acceptable? Any country you would not travel to?

5.3 Employment form: permanent, fixed-term, part-time, freelance, B2B/self-employed contract —
which are fine, which not?

5.4 Office presence: fully on-site, hybrid (how many days), fully remote — what do you prefer,
what do you accept, what do you refuse?

5.5 Working hours: shift work, weekends, a fixed 9-to-5 versus flexible — anything that is a no?

5.6 Anything else you would reject regardless of pay: company size, industry, language of the
team, management style, physical conditions?

## 6. Pay and company

6.1 Salary floor per location tier: Finland or Estonia, rest of Europe, Dubai or the ex-Eastern-bloc
countries. Give a monthly gross number or a range; say if you do not know.

6.2 Would you trade salary for equity, learning, or a famous name? How much?

6.3 Company size preference: under 20, 20–200, 200–2 000, larger. Does a startup's risk attract
or repel you?

6.4 Public sector, defence, gambling, crypto, adtech, tobacco, oil and gas: any you exclude on
principle?

6.5 Does the company's product matter to you, or is the engineering work enough?

## 7. Location

7.1 Which cities, concretely, would you move to tomorrow? Which only with a strong offer? Which
never?

7.2 Any constraint on moving: family, partner, lease, studies, a date before which you cannot
move?

7.3 For remote work: which time zones can you overlap with, and would you accept a US-hours
schedule for US pay?

7.4 Do you need visa sponsorship anywhere, or are there countries where you already have work
rights?

## 8. How you apply

8.1 How many applications a week can you realistically do well? (This sets how big the "apply this
week" band should be.)

8.2 What makes you skip a posting within ten seconds? Buzzwords, length, tone, a required
certificate?

8.3 What makes you want to apply within ten seconds?

8.4 Which of your past applications felt like the best match, and which rejection surprised you?
(These become worked examples for the ranker once you record decisions in the web UI.)

## 9. Scoring weights

Split 100 points across these criteria to say what matters most when two postings differ. Add
or rename criteria if these are wrong. This becomes `weights:` in the profile.

| criterion | what it measures | points |
|---|---|---|
| stack_fit | overlap between the posting's core tools and your fluent band | |
| domain | how much the problem domain is one you want | |
| level | how exactly the seniority asked for matches what you are | |
| location | location tier, remote terms, relocation effort | |
| pay | stated or likely pay against your floor | |
| company_type | product vs consultancy, size, industry, reputation | |

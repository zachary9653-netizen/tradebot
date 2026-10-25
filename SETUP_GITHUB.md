# Running the bot without your PC on

Your PC is only the machine the bot happens to live on. Moving it to GitHub
means GitHub's computers run it on schedule instead, so it works while your
PC is off, at school, or unplugged.

Everything on this side is done: the repository is created, the workflow is
written, and the first commit is made. What is left needs your GitHub
account, so it has to be you.

Five steps, about ten minutes.

---

## 1. Make a GitHub account

Skip if you have one. Go to <https://github.com/signup>. The free plan is
all this needs.

## 2. Create an empty repository

Go to <https://github.com/new> and set:

- **Repository name**: `tradebot`
- **Private**: selected. **This matters** - see the warning at the bottom.
- **Do NOT** tick "Add a README", ".gitignore", or "license". The repository
  must start empty or the next step will not push cleanly.

Press **Create repository**. Leave the page open; you need the URL.

## 3. Push your code

In the Tradebot folder, run these two commands, replacing `YOUR-USERNAME`
with your GitHub username:

```bash
git remote add origin https://github.com/YOUR-USERNAME/tradebot.git
```

```bash
git push -u origin main
```

Git will ask you to sign in to GitHub the first time. A browser window
opens - approve it there.

Your `.env` file is **not** uploaded. It is listed in `.gitignore`, and I
checked it was excluded before committing. Your keys stay on your PC.

## 4. Give GitHub your Alpaca keys

The bot cannot trade without them, and they must never be in the code. They
go in GitHub's encrypted secret storage instead.

On your repository page: **Settings** -> **Secrets and variables** ->
**Actions** -> **New repository secret**. Add two, one at a time.

| Name | Value |
| --- | --- |
| `ALPACA_API_KEY` | your key id, the shorter one |
| `ALPACA_SECRET_KEY` | your secret key, the longer one |

Copy them out of your local `.env` file. Type the names exactly as written -
they are case-sensitive, and a typo shows up as "unauthorized" later.

Once saved, GitHub will not show them to you again, and it masks them if
anything tries to print them into a log.

## 5. Test it, then turn off the PC schedule

On your repository page: **Actions** -> **Trading bot** -> **Run workflow**.
It takes about a minute. Green tick means it worked. Click into the run to
read the output - the bot's log is printed on the summary page.

If it fails, open the run and read the error. "unauthorized" means step 4's
secrets are wrong or misnamed.

**Once the cloud run works, turn off the PC schedule.** Otherwise two
separate bots are watching the same account and could both buy on the same
crossover, because each keeps its own memory of what it has traded:

```powershell
Disable-ScheduledTask -TaskName "Tradebot MA Crossover"
```

---

## Checking it from your phone

- **Actions tab** of your repository. Each run's summary page shows the
  bot's log - the averages, the signal, and any order placed.
- **Run workflow** on that same tab triggers the bot by hand, which works
  fine from a phone.
- **The dashboard** is attached to each run as a downloadable file called
  `dashboard`, kept for 30 days.

## When it runs

Weekdays at 9:30 AM Eastern, give or take.

GitHub's scheduler is best-effort and often starts 5 to 20 minutes late,
sometimes more when lots of people are queued. That is worth understanding
rather than worrying about: the signal comes from the **previous day's
close**, which cannot change while you wait, so a late start moves the price
you get filled at, not the decision the bot makes.

The workflow lists two times, 13:30 and 14:30 UTC, because GitHub's
scheduler does not follow daylight saving and the market open slides under
it twice a year. One of the two is the real 9:30 ET; the other runs an hour
early, where a market order simply waits for the open. `state.json` stops
the second run of the day trading the same crossover again.

## Cost

Free. Private repositories get 2,000 minutes of Actions a month, and these
runs take about a minute each, so roughly 22 a month against a 2,000 budget.

---

## Why private, and what public would cost you

Keep the repository **private**.

Your API keys are safe either way - they live in GitHub's encrypted secrets,
not in the code. The reason is the rest of it. A public repository publishes
your commit history to the open internet, and the bot commits `state.json`
and `logs/bot.log` after every run. That is a public record of what you
trade and when. There is no good reason to hand that to strangers.

You may read that free GitHub Pages could host the dashboard at a web
address you could bookmark. It could - but only from a **public**
repository, which would put your balance and account number online. If you
want that convenience, say so and I will strip the account details out of
the published page first. Until then, use the artifact download.

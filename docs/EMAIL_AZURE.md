# Sending the planner's emails with Azure (no-reply, at volume)

This guide sets up **Azure Communication Services (ACS) Email** so the planner sends from an
address like `noreply@mail.yourdomain.com` instead of your own inbox, and so it can send everyone's
morning plan at 7:00 or 8:00.

You do it in three stages. Stop after any stage and the app keeps working at that stage's limit.

| Stage | Sender address | Limit | Cost |
|---|---|---|---|
| 1. Test with an Azure address | `DoNotReply@xxxx.azurecomm.net` | 5 a minute, **10 an hour** (fixed) | Free resource, $0.00025 per email |
| 2. Your own domain | `noreply@mail.yourdomain.com` | 30 a minute, **100 an hour** to start | Domain ≈ $10–15 a year |
| 3. Higher limit (on request) | same | whatever Microsoft approves, e.g. 1,000 a minute | Still $0.00025 per email |

There's no paid tier to buy. ACS Email is pay-as-you-go: about **$0.25 per 1,000 emails**, plus
$0.00012 per MB (the planner's emails are ~15 KB, so that part is negligible). The "paid" parts
are your domain, and moving the web app to the **B1** plan (≈ $13/month) so it has enough CPU for
big morning sends.

> **Heads-up from Microsoft:** ACS (including Email) is being retired on **30 September 2028**.
> It's fully available until then: new resources, custom domains and limit increases are all still
> accepted. The planner also speaks plain SMTP, so moving to another provider later (Microsoft lists
> partners such as Infobip) is a change of settings, not code.

---

## How the app sends at 7:00

- Each person picks their own time in **Settings** (6:00 to 9:00, in their timezone). Spreading people
  across hours also spreads the load.
- The timer calls the app **every 5 minutes**. Each run sends in parallel batches, never goes over the
  per-minute and per-hour limits you configure, and leaves anyone over the limit for the next run a few
  minutes later. If Azure still says "slow down" (HTTP 429), the run stops and retries next time.
- Nobody gets the same email twice, however often the timer runs.
- Bounced addresses are reported back by Azure (stage 2, step 6) and stop getting scheduled mail.
  Microsoft checks your bounce rate before raising limits.

With your limit approved at **N emails a minute**, plan on roughly **N × 40** people in the first hour
after 7:00. For example, 500 a minute covers about 20,000 people by 8:00.

---

## Stage 1 — Test with an Azure address (15 minutes, free)

**1. Create the Email Communication Service**
Portal → **Create a resource** → search **Email Communication Services** → **Create**.
Resource group `planner-rg`, name `AI-planner-email`, Data location: pick the nearest (e.g. *India* if
listed, otherwise *Europe* or *United States*) → **Review + create** → **Create**.

**2. Add the free Azure domain**
Open `AI-planner-email` → **Provision domains** → **Add domain** → **Azure managed domain**.
After about a minute it shows a domain like `xxxxxxxx.azurecomm.net` with the sender
`DoNotReply@xxxxxxxx.azurecomm.net`.

**3. Create the Communication Services resource and connect the domain**
Portal → **Create a resource** → **Communication Services** → **Create**.
Resource group `planner-rg`, name `planner-acs`, same data location → **Create**.
Open `planner-acs` → **Email → Domains** → **Connect domain** → choose `AI-planner-email` and the
azurecomm.net domain → **Connect**.

**4. Copy the connection string**
`planner-acs` → **Settings → Keys** → copy **Primary connection string**
(`endpoint=https://planner-acs....communication.azure.com/;accesskey=...`).

**5. Tell the web app**
Web App → **Settings → Environment variables** → add these, then **Apply**:

| Name | Value |
|---|---|
| `ACS_CONNECTION_STRING` | the connection string |
| `EMAIL_FROM` | `DoNotReply@xxxxxxxx.azurecomm.net` |
| `EMAIL_RATE_PER_MINUTE` | `5` |
| `EMAIL_RATE_PER_HOUR` | `10` |

Remove any `SMTP_*` settings, so the app uses Azure.

**6. Try it**
Sign up on your site with your email → click **Confirm my email** in the welcome email →
**Settings → Email me today's plan**. Check spam the first time.

---

## Stage 2 — Your own domain, with a no-reply address

**1. Buy a domain**
Any registrar works: GoDaddy, Namecheap, Hostinger, Cloudflare (`.com` ≈ $10–15/yr, `.in`/`.dev`
often less). In Azure you can also buy one under **App Service Domains**, which keeps the DNS in
Azure; some student and free-credit subscriptions can't buy domains, so use a registrar if it's
greyed out.

**2. Add it to the Email Communication Service**
Use a **subdomain** such as `mail.yourdomain.com` for sending. It keeps the planner's email
reputation separate from anything else you do with the domain later.
`AI-planner-email` → **Provision domains** → **Add domain** → **Custom domain** → enter
`mail.yourdomain.com` twice → **Confirm** → **Add**.

**3. Prove you own it (TXT record)**
Click **Verify domain**. The portal shows a TXT record. At your registrar's DNS page (or Azure
**DNS zones** → your domain → **+ Record set**), add it **exactly** as shown → back in the portal
click **Next** → **Done**. DNS takes 15–30 minutes. When **Domain status** says *Verified*, continue.

**4. Let receivers trust your mail (SPF + DKIM + DMARC)**
Click **Configure** next to the domain. Add the records it shows, copying the names and values exactly:

| Type | Name (for `mail.yourdomain.com`) | Value |
|---|---|---|
| TXT (SPF) | `mail` | `v=spf1 include:spf.protection.outlook.com -all` |
| CNAME (DKIM) | `selector1-azurecomm-prod-net._domainkey.mail` | `selector1-azurecomm-prod-net._domainkey.azurecomm.net` |
| CNAME (DKIM2) | `selector2-azurecomm-prod-net._domainkey.mail` | `selector2-azurecomm-prod-net._domainkey.azurecomm.net` |
| TXT (DMARC, recommended) | `_dmarc.mail` | `v=DMARC1; p=none;` |

Some registrars add the domain automatically, so the *Name* is just the part before `yourdomain.com`.
Click **Next** → **Done**. Wait until SPF, DKIM and DKIM2 all say *Verified*.

Microsoft also recommends an **MX record** for the sending domain, even though the planner never
receives mail; a domain without one looks less trustworthy. Most registrars offer free email
forwarding: turn it on for `mail.yourdomain.com` and it adds the MX record for you.

**5. Create the no-reply sender and connect the domain**
`AI-planner-email` → **Provision domains** → click `mail.yourdomain.com` → **MailFrom addresses** →
**+ Add**: Display name `AI Execution Planner`, MailFrom `noreply` → **Save**.
(The display name people see comes from here, not from the app.)
Then `planner-acs` → **Email → Domains** → **Connect domain** → pick `mail.yourdomain.com`.

Update the web app settings, then **Apply**:

| Name | Value |
|---|---|
| `EMAIL_FROM` | `noreply@mail.yourdomain.com` |
| `EMAIL_RATE_PER_MINUTE` | `30` |
| `EMAIL_RATE_PER_HOUR` | `100` |

**6. Report bounces back to the app (Event Grid)**
This stops mail to dead addresses automatically, and Microsoft asks about it when you request more capacity.

1. Make up a long random key (Cloud Shell: `python3 -c "import secrets;print(secrets.token_urlsafe(32))"`)
   and add it to the web app as `EMAIL_EVENTS_KEY`. **Apply**.
2. If you've never used Event Grid: **Subscriptions** → your subscription → **Resource providers** →
   `Microsoft.EventGrid` → **Register**.
3. `planner-acs` → **Events** → **+ Event Subscription**:
   - Name `planner-bounces`, Event schema **Event Grid Schema**, System topic name `planner-acs-events`
   - Filter to event types: **Email Delivery Report Received** only
   - Endpoint type **Web Hook** → endpoint
     `https://<your-url>/email/events?key=<EMAIL_EVENTS_KEY>` → **Create**

   Azure checks the endpoint as it creates the subscription; the app answers that automatically.
   The first 100,000 events a month are free.

**7. Move the web app to B1 (recommended for big sends)**
Web App → **App Service plan → Scale up** → **B1** → **Apply**, then **Configuration → General
settings → Always on: On**. The Free plan's 60 CPU-minutes a day isn't enough to plan and email
thousands of people every morning.

---

## Stage 3 — Ask Microsoft for a higher limit

**1. Send real email for a week or two first.** Approval depends on your sender reputation:
delivery failures must stay **below 2%**, with few spam complaints. Confirmed addresses, one-click
unsubscribe and bounce handling (all built in) keep you there.

**2. Open the request.** Portal → **Help + support** → **Create a support request**:
- Issue type: **Service and subscription limits (quotas)**
- Subscription: yours. Quota type: **Azure Communication Services Email: Sending Limits** (if it isn't
  listed, choose *Technical* → *Communication Services* → *Email*)
- Fill in the details using the template below. Quota requests don't need a paid support plan. Some
  free-credit subscriptions can't open them; upgrading to *Pay-As-You-Go* (Subscriptions → Upgrade)
  fixes that.

**3. Paste this, with your details filled in:**

```
Customer Information
Company name: <your name> (personal project)
Company website: https://<your-url>
Business description: AI Execution Planner, a web app that plans each user's day from their goals
and calendar, and emails them their plan each morning.

Email Service Information
Subscription ID: <Subscriptions → copy ID>
Azure Communication Services Resource Name: planner-acs
Custom domain set up and in use: Yes
Sending domain: mail.yourdomain.com

Usage Information
1. Type of emails: Transactional (account confirmation, the user's own daily plan, and goal
   deadline alerts the user opted into). No marketing.
2. Expected volume:
   - Max per minute: <e.g. 500>
   - Max per hour: <e.g. 20,000>
   - Max per day: <e.g. 40,000>
   Most mail is sent between 06:00 and 09:00 in each user's timezone.

Additional Information
Source of addresses: users sign up themselves and must confirm their address through a link
before any scheduled email is sent (double opt-in).
Unsubscribes and bounces: every email has a one-click unsubscribe link (also as a List-Unsubscribe
header). Users can turn each email off in Settings. Delivery reports come in through Event Grid, and
addresses that bounce or are suppressed are automatically removed from scheduled sending.
```

**4. When it's approved**, set the web app to match what you were given:

| Name | Value |
|---|---|
| `EMAIL_RATE_PER_MINUTE` | your approved per-minute limit |
| `EMAIL_RATE_PER_HOUR` | your approved per-hour limit |
| `EMAIL_CONCURRENCY` | roughly per-minute ÷ 100, between 4 and 16 (each worker sends ~100–150 a minute) |

---

## Check it's working

- **Web App → Log stream** at 7:00: you'll see runs finishing and, if you're over the limit,
  `N emails deferred to the next run`.
- Run the timer yourself:
  `curl -X POST -H "X-Cron-Secret: <CRON_SECRET>" https://<your-url>/internal/cron/hourly`
  returns e.g. `{"emails_sent": 30, "deferred": 120, "throttled": false, ...}`.
- **planner-acs → Monitoring → Insights** (once Diagnostic settings send logs to a Log Analytics
  workspace) shows delivered, bounced and failed counts.

| Problem | Fix |
|---|---|
| `sending email to … failed` with *DomainNotLinked* or *InvalidSenderUserName* | The domain isn't connected to `planner-acs` (stage 2 step 5), or `EMAIL_FROM` doesn't match a MailFrom address exactly. |
| Lots of `deferred`, every run | You're at your limit: that's expected. Request more (stage 3), or let people spread across 6:00–9:00. |
| `throttled: true` | Azure returned 429. Lower `EMAIL_RATE_PER_MINUTE` to what you were actually approved for. |
| Mail lands in spam | Check SPF, DKIM and DKIM2 all show *Verified*, add DMARC and an MX record, and send for a while to build reputation. |
| Event subscription won't create | The web app must be running and `EMAIL_EVENTS_KEY` set *before* you create it; check the endpoint URL and key. |

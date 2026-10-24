# WRCCDC Blue Team Cheat Sheet

## Open These Tabs First

| What | URL | Login |
|---|---|---|
| **Portal** (Packets, Tickets, Incident Reports) | `https://portal.wccomps.org` | Shared team login (`teamXX`) |
| **Quotient** (Service Uptime & Injects) | `https://scoring.wccomps.org` | Shared team login (`teamXX`) |
| **Proxmox Console** (VMs) | `https://competitors.wccomps.org` | Shared team login (`teamXX`) |
| **Jump Box** (Browser Terminal / SSH) | `https://jumpbox.wccomps.org` | Shared team login (`teamXX`) |

> **Before Competition Day (Tue–Thu):** Your team packet (with your `teamXX` login) is emailed out **days before the Saturday competition** (usually Tuesday, Wednesday, or Thursday). **Run `/link` in Discord as soon as your packet arrives** so your whole team has access to your private Discord channels and `portal.wccomps.org/packet/` before Saturday morning!

---

## Where Your Score Comes From

- **40% Service Uptime (Quotient):** Automated checks every few minutes. Consecutive failures trigger extra **SLA penalties**.
- **40% Business Injects (Quotient):** Watch deadlines—Quotient locks late submissions automatically.
- **20% Orange Team (Live Humans):** Volunteers acting as real customers/users during timed windows.
  - **Trap:** Only a few Orange checks happen, so **each Orange check is worth a massive chunk of your total score**. If you firewall off the network or break a web app so a human can't use it, it hurts badly.

---

## Don't Shoot Yourself in the Foot

1. **Changed a password? Update Quotient:** If a scored service uses credentials and you change the password on the box without updating it in Quotient, your service check will fail.
2. **Protect PII (`-200 pts`) and Root (`-100 pts`):** Red Team deducts flat points per box/compromise (`-25` user, `-25` sensitive files/encrypted DB, `-50` creds/credit cards, `-100` root/priv-esc, **`-200` PII**).
3. **Don't Lock Out Customers:** A port returning a blank `200 OK` might pass a basic check, but Orange Team grades whether a real user can actually complete their task.
4. **Stay in Scope:** Never scan or attack other teams, Quotient, or competition infrastructure.

---

## Got Hacked? Get 80% of the Points Back

Submit an **Incident Report** at `portal.wccomps.org/scoring/incident/submit/` to recover **up to 80%** of Red Team deductions.

**What to include (vague reports get `0`):**
- **Attacker Source IP** + **Target Box / Service** (click the box button to auto-fill your `10.100.1X.*` IP)
- **When** you detected it + whether it's **mitigated**
- **What happened:** How they got in, what accounts/data they touched, and how you fixed it
- **Proof:** Attach logs, screenshots, PDFs, or archives (up to 20 files, 50MB each)

---

## Need Help? Support Tickets (`/ticket` or `portal.wccomps.org/tickets/`)

| Request | Cost | When to Use |
|---|---|---|
| **Service Scoring Validation** | **Free** | Service works & Quotient creds are updated, but check still fails |
| **Scoring Service Check** | **`10 pt`** | Paid manual check of a service |
| **Box Reset / Scrub** | **`60 pt`** | Box is bricked or owned beyond repair—staff re-images it |
| **Black Team Phone Consult** | **`100 pt`** | Voice troubleshooting help from Black Team |
| **Black Team Hands-on Consult** | **`200 pt`** *(300 pt >45m)* | Black Team logs in to help fix a box |
| **Other / General Issue** | **Variable** | Infrastructure or rules question |

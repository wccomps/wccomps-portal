# Red Team Submission Guide

How to submit and manage your findings in the WCComps scoring portal.

## Submitting a Finding

Navigate to `/scoring/red-team/` and fill out the form.

### 1. Attack Details

- **Attack Type** — pick from the dropdown (e.g., RCE, SQL Injection, Brute Force)
- **Source IP** — either enter a single IP address, or select one of your IP pools if you're rotating through multiple addresses

### 2. Target Information

- **Affected Boxes** — click the buttons for the systems you compromised. Target IPs and available services auto-populate based on your selection.
- **Affected Service** — which service on the box was exploited (dropdown filters based on selected boxes)

### 3. Attack Characteristics

- **Universally attempted** — check this if you tried the attack against all teams
- **Persistence established** — check this if you maintained persistent access

### 4. Attack Outcomes

Check all that apply. These determine point deductions per affected team:

| Outcome | Deduction |
|---|---|
| Root/Admin access | -100 |
| User access (only scored if no root) | -25 |
| Privilege escalation | -100 |
| Credentials recovered | -50 |
| Sensitive files recovered | -25 |
| Credit cards recovered | -50 |
| PII recovered | -200 |
| Encrypted DB recovered | -25 |
| DB decrypted | -25 |

The total deduction per team updates in real time as you check boxes.

### 5. Affected Teams

Click team number buttons to select which teams were compromised. "Select All" and "Deselect All" buttons are available.

### 6. Evidence

Upload screenshots, logs, or other proof — up to 20 files, max 50MB each.

### 7. Notes

Free text for additional context about the attack.

## Viewing Your Findings

| Page | URL |
|---|---|
| All findings | `/scoring/red-team/scores/` |
| Single finding | `/scoring/red-team/score/<id>/` |

The findings list supports filtering by status (pending/approved), team, attack type, and submitter.

You can **delete** your own findings as long as they haven't been approved yet.

## IP Pool Management

If you're rotating through multiple source IPs, you can save them as reusable pools instead of entering a single IP each time.

| Action | URL |
|---|---|
| View your pools | `/scoring/red-team/ip-pools/` |
| Create a pool | `/scoring/red-team/ip-pools/create/` |
| Edit a pool | `/scoring/red-team/ip-pools/<id>/edit/` |
| Delete a pool | `/scoring/red-team/ip-pools/<id>/delete/` |

You can also create a new pool on the fly from the submission form using the "Create Pool" button.

## What Happens After You Submit

1. Your finding is created
2. Gold Team reviews the finding
3. Once approved, the point deductions are applied to the affected teams' scores

## API / Scripted Submissions

There is no dedicated REST API for submitting findings. Submission is form-based and requires browser authentication with CSRF tokens.

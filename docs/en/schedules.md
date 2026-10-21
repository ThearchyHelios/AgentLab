English | [Simplified Chinese](../zh-CN/schedules.md)

# Schedules, Event Triggers, and Notifications

This page covers how to run workflows automatically on a schedule or when an event happens, and how results and reminders reach people. It is for builders and admins. The user interface is currently available in Simplified Chinese only.

## Schedules

A schedule starts formal runs on time, and the origin of each run is recorded as the schedule.

- The target can be a workflow, a data source (that is, scheduled maintenance of the data source; see [Data Catalog](data-catalog.md#scheduled-maintenance)), or a test suite (that is, a scheduled evaluation; see [Regression evaluation](evaluation-and-ledger.md#scheduled-evaluations)).
- App views prefer to bind to existing schedules. A dashboard reads the schedule's latest sealed formal run, and its stamp names the schedule and the run number.
- When a run is rejected by a person, the schedule pushes "Not issued (rejected)".
- When a run finishes successfully and is sealed, it is compared once automatically with the same schedule's last successful run, and the trigger history offers Compare With Previous Run (see [Run comparison](evidence.md#run-comparison)).
- Schedules that target a data source or a test suite don't accept webhooks (they return 422); their notifications go through channel subscriptions under Settings → Notifications.
- For run concurrency, schedules belong to the scheduled class (see [Run concurrency](postgresql-and-scaling.md#run-concurrency)).
- The scheduler loop ticks every `AGENTLAB_SCHEDULER_TICK_S`, 15 seconds by default.

## Event triggers

A schedule can choose Event trigger and run when one of these events happens:

- an uploaded spreadsheet gets a new period;
- a formal run of an upstream workflow finishes successfully;
- a data source has changes that need action.

Rules:

- Events are recorded where they happen, and the scheduler loop starts runs after debouncing; each period is debounced separately.
- Downstream input templates read the event content with `{{ event.* }}`.
- Saving checks that chains don't form a cycle; at run time, chains are limited to 5 levels.
- Runs that passed through a rejection, and runs that come from app backfills, don't trigger downstream by default; you can tick a box to let them.
- This follows the `AGENTLAB_SCHEDULER` switch: when the switch is off, events are kept but not dispatched.

## Notifications

### Channels

Notifications go to channels, which are either global or personal; subscribe to them under Settings → Notifications. Webhooks can be delivered too.

Tasks, notifications, and webhooks state plainly where a rejection went ("Rejected, routed to X", "This path ends here").

### Email

An admin configures SMTP under Settings → Notifications → Mail Service; the password is stored encrypted. After that, email channels can be created.

- Email only confirms handoff to the mail server; bounces aren't tracked.

### Subscribable events

| Event | Sent to | Notes |
|---|---|---|
| "Instance finished" | Subscribed channels | Includes runs rejected by a person |
| "Scheduled evaluation regressed" | Subscribed channels | Only the test suite name, pass count, number of regressed questions, and a link |
| "Data source has changes to handle" | Global channels | Only the source name, the count, and a link; no table names, column names, or values |
| "Caliber version bumped, answer numbers changed" | Global channels and the answer owner's personal channels | No numbers; `ledger.recall` is pushed at the same time |
| Daily task digest | Personal channels subscribed to it | Sent at `notify.digest_at`, default 09:00 (server time zone) |
| Access key expiry reminder | Personal keys go to the owner's personal channels subscribed to it; service account keys go to global channels subscribed to it | Once 7 days and once 1 day before expiry; only the key name, its prefix, the owner, the expiry time, and a link to rotate it, never the key itself |

The daily digest and access key reminders are sent only when login is on.

## The scheduler switch

When `AGENTLAB_SCHEDULER` is turned off (set to 0):

- process time limits aren't checked: no reminders, no escalation, no automatic verification;
- events are kept but not dispatched;
- the retroactive caliber matching of the last 7 days at startup doesn't run, and neither do the automatic caliber recall batches after publishing or their continuation at startup.

In a multi-process deployment, claiming schedules, dispatching events, delivering webhooks, process time limits and verification, and the daily digest all happen only in the process that is the scheduler leader (see [Scheduler leader](postgresql-and-scaling.md#scheduler-leader)).

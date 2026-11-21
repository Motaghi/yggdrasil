# PRINCE2 in Yggdrasil

Yggdrasil's flow is inspired by PRINCE2®, a project method built around stages, approvals and managing by exception, and maps its ideas onto agents with as little ceremony as still keeps you in control. It is not an implementation or certification of PRINCE2, and it works for any kind of project: code, writing, research or business tasks.

PRINCE2® is a registered trademark of PeopleCert group. Yggdrasil is not affiliated with or endorsed by PeopleCert.

You don't need to know PRINCE2 to use Yggdrasil. By default it speaks plain English, and this page is for readers who want to see how its ideas map.

## Roles

You are the Executive and the whole Project Board. The user and supplier interests a board normally splits between people are held by you alone, a deliberate tailoring for one person. The project manager session is the Project Manager. Team leads are Team Managers. User-side reviewers carry user assurance and, in agile projects, act as product owner.

## Principles

| PRINCE2 principle | How Yggdrasil maps it |
|---|---|
| Continued business justification | A short business case in every project plan (why, expected benefit, cost in elapsed time and your minutes, main risks, and a "stop if" condition). The project manager re-checks it at every phase end; if it no longer holds, that is a problem needing your call. A plan may skip it with a one-line reason. |
| Learn from experience | Lessons reports from team leads, a lessons log per project, and lessons from closed projects cited in new plans. |
| Defined roles and responsibilities | The levels, the pairing of team leads and reviewers, and the message rules that `ygg` checks. |
| Manage by exception | Limits per project; concerns go to the project manager, problems to you; autonomy envelopes. |
| Manage by stages | Phases with an approval at each end. |
| Focus on products | Product descriptions in the plan, criteria written before work starts, and acceptance that names the exact commit tested. |
| Tailor to suit the project | Delivery approach and cadence per plan, the collapsed shape for small projects, profiles, and the versioned tracker standard. |

## Practices

| Practice | Mechanism |
|---|---|
| Business case | The plan's `business_case`, re-checked at every phase end. |
| Organizing | Levels, pairing, the message rules and domains. In agile projects the reviewer proposes backlog order, the project manager orders within the plan, and you can set priorities and themes at any time. |
| Plans | The project plan, phase plans and the route map; for agile projects also the backlog and, with timeboxes, the timeboxes. |
| Quality | Quality methods per product, criteria first, a review loop by the configured pair model (from the other family under the default mixed setup), reviewer acceptance and CI. |
| Risk | Risk records, the route map's list of known unknowns, the risk limit, and the business case's risks and "stop if". |
| Issues | Concern, problem, forecast problem and decision records. |
| Progress | Limits, check-ins and progress reports, the route map forecast, the daily digest and `ygg status`. |

## Processes

| Process | What happens |
|---|---|
| Starting up a project | Your main session interviews you into a project brief. |
| Directing a project | Your decisions through the main session: approving the plan and phases, deciding problems, granting envelopes, ordering the backlog, closing the project. |
| Initiating a project | The project manager writes the plan and the first route map. |
| Controlling a stage | Authorising work items (respecting the work-in-progress limit), check-ins, concerns and problems. |
| Managing product delivery | Team leads, workers, the review loop, marking work complete, and reviewer acceptance. |
| Managing a stage boundary | Phase review, next phase plan, the business-case check and backlog re-prioritisation. |
| Closing a project | End-project report, lessons and `ygg project close`. |

## Delivery approach

Each project plan says how the work is delivered, in `delivery_approach`. If it doesn't, the default is agile with a continuous flow.

| | Waterfall | Agile, flow (default) | Agile, timebox | Custom |
|---|---|---|---|---|
| Phases | One after another | Phases | Phases, each cut into timeboxes of `timebox_hours` | As the plan describes |
| Planning | Work items from product descriptions written up front | Work items pulled from a prioritised backlog | Work items from the backlog, re-prioritised at each timebox end | As described |
| Limits | The plan's limits | The plan's limits, plus an optional `wip_limit` on unfinished work items | Time is fixed at timebox ends; scope flexes (`could` and `should` items move, a `must` item needs your call) | The plan's limits |
| Review | Per work item and at phase ends | Per work item and at phase ends | Also a check-in, a reviewer demo and backlog re-prioritisation at each timebox end | Per work item and at phase ends |

All approaches run on the same machinery: the same levels, approvals and envelope rules. A custom approach needs a `delivery_description`.

## Plain words and PRINCE2 terms

Set `vocabulary: prince2` to see PRINCE2 terms instead of plain ones. This changes only the labels in the `ygg status` text and the main session's wording. Command names, JSON output and file formats stay the same.

| Concept | Plain (default) | PRINCE2 |
|---|---|---|
| PID | project plan | PID |
| Stage | phase | stage |
| Highlight report | progress report | highlight report |
| Checkpoint report | check-in | checkpoint report |
| Exception | problem needing your call | exception |
| Issue | concern | issue |
| Work package | work item | work package |
| End-stage report | phase review | end stage report |
| Senior user | user-side reviewer | senior user |
| Team lead | work lead | team manager |
| Project state `initiating` | being planned | initiating |
| Project state `pid-ready` | project plan ready for you | PID ready |
| Project state `executing` | in progress | executing |
| Project state `awaiting-approval` | waiting for your approval | awaiting approval |

Commands keep their PRINCE2-based names (`ygg pid approve`, `ygg stage end`, `ygg wp authorise`) whichever vocabulary you choose.

# The consumer app

The household version of the home monitor, provided by the ISP under its own brand
(`controller/web/app.html`, served at `/app/`; the lab brand is the fictional "SampleNet Fibre").
Its home screen leads with **the line**: the plan's capacity drawn as a pipe that fills with what each
device is using right now, under one plain sentence ("Smooth for everyone", "Busy, still smooth").
Sign-in is the broadband account's mobile number and a one-time code (in the lab the code is shown on
screen because there is no SMS gateway).

## What subscribers expect from their provider's app (researched)

| Feature | Seen in | Here |
|---|---|---|
| "Is my internet OK?" at a glance | eero Activity, Plume HomePass, Calix CommandIQ | Status hero in plain words from the delay grade; "router offline" with what to check |
| Outage / area issue status | Calix (service status via CommandIQ); JioHome reviews complain of issues and support tickets left unresolved | Banner when the controller has an incident on the subscriber's OLT; restoration message; TRAI 4-hour rebate rule |
| See connected devices, name them | MyJio device page (search, filter, block/unblock), JioHome (connected devices), Airtel Thanks "My Wi-Fi" | Devices list with what each is doing, speed now and data today; rename and set type |
| Pause / block a device | MyJio (block/unblock), Calix ExperienceIQ, Plume Control | Pause for 15 min, 1 h, 3 h or until resumed (enforced in nftables on the router) |
| Prioritise a device or activity, for a while | Plume Network Priority (boost for 3 h, 6 h or end of day; extend or cancel), Calix MyPrioritiesIQ (Work, Gaming, Streaming), Google Nest | **Boost**: device, purpose, duration; a preview of what is reserved and who uses what is left; live "Working / Not keeping up" status; quick modes; weekday schedules |
| Speed test | JioHome, eero, Plume | Speed test run **from the router** across the access line (Wi-Fi does not skew it), with extra delay while busy and the plan rate |
| Report a problem and follow it | Calix (unified support), JioHome reviews (unresolved tickets) | Report with category; the controller's diagnosis returns in plain words at once; ticket thread with replies from support |
| Data usage | JioHome / MyJio, eero | 30-day usage per device from the router |
| Plan, price and renewal; ask for a different plan | MyJio and Airtel Thanks (plan card with validity, upgrade) | Plan card with monthly price and renewal date; *Change plan* lists the plans and sends a request that opens a ticket (the ISP calls to confirm; nothing changes until the subscriber agrees) |
| The app in the subscriber's language | MyJio, Airtel Thanks, JioHome (Hindi and other Indian languages) | English and हिन्दी throughout (switch in the top bar or Account), Mukta typeface for both scripts, numbers and dates in the Indian format |
| Privacy controls and transparency | DPDP Act 2023 and DPDP Rules 2025 (notice, granular consent, withdrawal as easy as consent, access, erasure, grievance within 90 days) | Separate toggles for quality improvement, support diagnostics and offers; "See my data" with **who accessed my data** (from the audit log) and a full JSON download; erasure and grievance requests with 30-day due dates; diagnostic sessions only after an in-app "Allow for 60 minutes" |

Out of scope for this prototype, but expected in a full ISP app: Wi-Fi name and password, guest
network, parental content filters and bill payment. On an OpenWrt router the first two
are UCI settings the agent could change through the same signed-command channel.

## How it works

The app talks only to the controller (`/v1/app/*`). Plan, status, incidents, tickets, notifications
and privacy come from the controller. Live views (devices, boosts, usage, schedules) and actions
(boost, pause, rename, schedules) are relayed to the subscriber's own router through its agent and are
not stored by the controller; each action is recorded in the audit log the subscriber can see.

## Sources

- Plume Network Priority: https://www.plume.com/newsroom/network-priority-gives-subscribers-more-control
- Plume HomePass modules: https://www.plume.com/legal/product-descriptions
- Calix CommandIQ: https://www.calix.com/products/smartlife/smarthome/commandiq.html
- Calix HomeOfficeIQ: https://www.calix.com/products/smartlife/smarthome/homeofficeiq.html
- MyJio JioFiber device management: https://onlytech.com/myjio-app-revamps-jiofiber-device-management-page/
- JioHome (App Store listing and reviews): https://apps.apple.com/in/app/jiohome/id1477828232
- Airtel Thanks Wi-Fi settings: https://www.giznext.com/news/how-to-change-airtel-xstream-wi-fi-password-via-thanks-app/
- DPDP Rules 2025 summary: https://tsaaro.com/blogs/dpdp-rules-2025-explained-full-overview-and-practical-summary
- TRAI 2024 broadband QoS norms: https://www.medianama.com/2024/08/223-trai-tightens-quality-norms-for-broadband-networks/

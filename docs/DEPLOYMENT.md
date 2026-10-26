# Deployment

Three things are deployed: the **router package** on every gateway, the **controller** at the ISP,
and the **consumer app** (served by the controller). The router works on its own; the controller adds
fleet management.

## 1. Router

### OpenWrt / prplOS (opkg)

```sh
# on the build machine
make router ipk                      # router/dist/*.ipk
scp router/dist/intentqos_1.0.0-1_all.ipk router/dist/iq-capture_1.0.0-1_aarch64_cortex-a53.ipk root@192.168.1.1:/tmp/
# on the router (pick the iq-capture package for its CPU: `opkg print-architecture`)
opkg update && opkg install /tmp/iq-capture_*.ipk /tmp/intentqos_*_all.ipk
uci set intentqos.engine.wan='wan'            # your WAN device: eth0, wan, pppoe-wan ...
uci set intentqos.engine.down_mbit='200'; uci set intentqos.engine.up_mbit='50'
uci commit intentqos && /etc/init.d/intentqos restart
logread -e intentqos                           # engine, watchdog, agent output
```

The init script renders `/etc/config/intentqos` into `/var/etc/intentqos.toml` (`intentqos-uci`) and
runs the engine, the watchdog and, if enabled, the agent as three procd instances with respawn.

To connect a router to the operator's controller (normally pre-set in the operator's firmware image):

```sh
uci set intentqos.agent.enabled='1'
uci set intentqos.agent.controller_url='https://qos.isp.example'
uci set intentqos.agent.claim_code='<from the label>'
uci set intentqos.agent.release_pubkey='<operator release key, hex>'
# operator CA certificate at /etc/intentqos/operator-ca.pem
uci commit intentqos && /etc/init.d/intentqos restart
```

The console and API need the **router key** (`api.require_auth`, on in the package): it is created on
first start in `/etc/intentqos/admin.key` (0600), like the admin password printed on a router's label,
and the console asks for it once per browser session. The agent reads the same file.

Checks: `/etc/init.d/intentqos status`; `tc -s qdisc show dev br-lan` shows `cake` or `htb`;
`nft list table inet intentqos`; `curl http://192.168.1.1:8080/api/setup-check`.

### prplOS LCM / any container runtime

`router/container/Dockerfile`: Alpine + Python, the engine, the model and the static helper. Run with
the host network, `CAP_NET_ADMIN` and `CAP_NET_RAW`, and a mounted `/etc/intentqos`.

### Generic Linux gateway (systemd)

`deploy/intentqos.service` and `deploy/intentqos-watchdog.service` (hardened: only `CAP_NET_ADMIN`
and `CAP_NET_RAW`). Add an agent unit the same way with `-m intentqos.agent`.

## 2. Controller

```sh
python3 -m controller.pki --dir controller/data/pki --dns qos.isp.example   # or use the operator's PKI
python3 -m controller.seed --sim 0                 # plans, staff accounts, profiles, signed models
python3 -m controller.server --config controller/controller.toml
```

- Listens on HTTPS 8443 (`listen`, `tls_cert`, `tls_key`). Put it behind the operator's load balancer.
- The release signing key is created at `controller/data/release.key` (0600). Its public half goes into
  the router image (`release_pubkey`). In production keep it in an HSM/KMS.
- Provision gateways from the BSS: `POST /v1/nb/gateways {"serial", "claim_code", "account"}`;
  subscribers and plans: `POST /v1/nb/subscribers {"account", "name", "phone", "plan", "olt", ...}`
  (`Authorization: ApiKey <key>`). A plan change re-configures that router automatically.
- Staff accounts: Admin view → Staff. Roles: viewer, support, noc, admin.
- Retention: raw telemetry 48 h, 5-minute rollups 90 days, events 90 days (`[retention]`).
- `demo_otp = false` and an SMS gateway are required outside the lab; leave `lab_quick_login` and
  `ui_listen` unset (they default to off).

## 3. Consumer app

Served by the controller at `/app/`, branded from `[brand]` in `controller/controller.toml`. Subscribers
sign in with the mobile number on their account. Wrap it in the ISP's existing app as a web view, or
call the same `/v1/app/*` API from a native app.

## Everything at once, on Windows

`dist/IntentQoS Demo (all three).exe` (or one part's exe) installs the release into WSL 2 and opens the
three consoles; `Stop IntentQoS.exe` stops it. Details and building: [`launcher/README.md`](../launcher/README.md).

## Everything at once, in the lab

```sh
sudo scripts/stack.sh up     # controller, 240 simulated homes, testbed, engine, agent
# http://localhost:8088/ops/    staff (role buttons on this computer; accounts in controller/data/lab-credentials.txt)
# http://localhost:8088/app/    subscriber (lab button, or 9800000001 with the code shown on screen)
# http://localhost:8090/        the lab home's own console, with traffic and link controls
sudo scripts/stack.sh down
```

The lab gateway's agent reaches the controller over the management link (10.255.0.1); its config is
the `[agent]` section of `config/testbed.toml`.

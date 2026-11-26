# Windows launchers

Six double-click programs for Windows 10/11. They run IntentQoS inside WSL 2, because the gateway
engine needs Linux network namespaces, `tc` and `nftables`; nothing is emulated or mocked on the
Windows side.

| Program | Starts | Opens |
|---|---|---|
| `IntentQoS Gateway.exe` | the emulated home network and the real engine, watchdog and agent | the gateway console, http://localhost:8090/ |
| `IntentQoS Operator Console.exe` | the fleet controller and 240 simulated homes | http://localhost:8088/ops/ |
| `SampleNet Home App.exe` | the same controller | the subscriber app, http://localhost:8088/app/, in a phone-sized window |
| `IntentQoS Demo (all three).exe` | everything | all three windows |
| `IntentQoS Video Demo (no router).exe` | everything, like the demo | the same three windows; the gateway console plays the 80 s scripted demo by itself (`#autoplay`), with a caption per step |
| `Stop IntentQoS.exe` | stops everything and closes its windows | a confirmation |

## What happens when you double-click

1. It finds a WSL 2 distribution (Ubuntu first). Without one it offers to install it (`wsl --install -d Ubuntu`,
   one administrator prompt, then a Windows restart and Ubuntu's one-time user setup).
2. It opens its window at once and shows progress there.
3. If the release inside the exe differs from the one in `/opt/intentqos`, it copies it into WSL and
   runs `wsl-setup.sh`: unpack, keep the controller database, keys and lab data, and install only
   missing Linux tools (`iproute2`, `nftables`, `openssl`; Python 3.11 on Ubuntu 22.04). There is no
   pip step: the engine, agent and controller use only the Python standard library.
4. It keeps one hidden WSL session open so WSL does not stop the distribution when the launcher exits,
   runs `scripts/stack.sh up <part>` (with systemd, as on Ubuntu, each service runs as a transient unit
   `iq-controller-…`, `iq-simfleet-…`, `iq-labd-…`, so it outlives the command that started it), waits until
   the service answers and switches the window to it. If a service stops or does not answer, the window
   shows its status, the end of its log and recent kernel messages (`stack.sh diag`).

The consoles are served over plain HTTP on `127.0.0.1` only (the controller's `ui_listen`), so there
is no certificate warning; routers and remote staff use the HTTPS listener on 8443. Lab sign-in shows
role buttons and works only from this computer (`lab_quick_login`, off by default in the code).

Logs: `%LOCALAPPDATA%\IntentQoS\launcher.log` on Windows, `/opt/intentqos/results-lab/logs/` in WSL.
Each window uses its own browser profile in `%LOCALAPPDATA%\IntentQoS\browser\<part>` (Edge, else
Chrome, else your default browser) and is sized to fit the screen; the demo puts the app on the right
and the two consoles, cascaded, beside it.

## Building

```
python3 launcher/build.py            # needs Go 1.21+; runs on Linux, macOS or Windows
```

It zips the project into `launcher/go/app.zip` (tracked files, LF line endings, without docs images
or run logs), embeds it with `wsl-setup.sh` and `starting.html`, and cross-compiles `launcher/go` five
times with a different `main.part`. With `rsrc` (github.com/akavel/rsrc) on PATH or in `$RSRC`, each
exe also gets its icon. Output: `dist/*.exe`, about 9 MB each, no Python or other runtime needed on
Windows.

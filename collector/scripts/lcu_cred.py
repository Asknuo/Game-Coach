"""Extract LCU credentials from LeagueClient process. Called by Go collector."""
import psutil
import sys

PROCS = {"LeagueClient.exe", "LeagueClientUx.exe"}

for proc in psutil.process_iter(["name", "pid"]):
    if proc.info["name"] in PROCS:
        try:
            cmdline = proc.cmdline()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        # cmdline is a list of args; join and parse
        joined = " ".join(cmdline)
        app_port = ""
        auth_token = ""
        for arg in cmdline:
            if arg.startswith("--app-port="):
                app_port = arg.split("=", 1)[1]
            elif arg.startswith("--remoting-auth-token="):
                auth_token = arg.split("=", 1)[1]
        if app_port and auth_token:
            print(f"{app_port}|{auth_token}")
            sys.exit(0)

sys.exit(1)

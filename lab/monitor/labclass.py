"""Explicit process roles for the agent lab. Shared by the live sampler and offline analysis.

Every process gets a named role; there is no catch-all bucket. Roles:
  agent              the harness process itself (matched by the adapter's agent_match regex)
  agent:nested       an agent process started by another agent (subagents)
  tool:shell         a shell the agent spawned for a tool call (bash -c ...)
  tool:command       a program run by a tool call (python3, seq, uname, apt-get, ...); label = its name
  tool:search        rg / fd spawned by grep/find tools
  tool:sudo          the sudo binary inside a tool call
  tool:root          a program running as root under sudo
  agent:helper       a direct child of the agent that is not a tool call (tmux queries, git, which, ...)
  tui:tmux           the tmux server/client hosting an interactive TUI run
  lab:docker-exec    runc processes behind each `docker exec`
  lab:harness        commands the lab runner itself executed (cp, ps, chown, setup scripts)
  container:init     docker-init and the container's keep-alive `sleep infinity`
  unclassified:<comm>  only if none of the above match (should not happen; shown with its name)
"""
import re

SHELLS = {"bash", "sh", "dash", "zsh", "fish"}
SEARCH = {"rg", "fd", "fdfind"}

ROLE_INFO = {
    "agent": ("Agent harness", "#58a6ff"),
    "agent:nested": ("Nested agent (subagent)", "#79c0ff"),
    "tool:shell": ("Tool shell (bash -c)", "#f0883e"),
    "tool:command": ("Tool command", "#ffa657"),
    "tool:search": ("Search tool (rg/fd)", "#a371f7"),
    "tool:sudo": ("sudo", "#f85149"),
    "tool:root": ("Command as root (via sudo)", "#ff7b72"),
    "agent:helper": ("Agent helper process", "#56d4dd"),
    "tui:tmux": ("TUI host (tmux)", "#d29922"),
    "lab:docker-exec": ("docker exec (runc)", "#6e7681"),
    "lab:harness": ("Lab runner command", "#3fb950"),
    "container:init": ("Container init", "#8b949e"),
}


def base(path):
    return (path or "").rsplit("/", 1)[-1]


class Classifier:
    """procs: {pid: {pid, ppid, comm, uid, cmd, first_exec, last_exec, nspid}}"""

    def __init__(self, agent_match):
        self.agent_re = re.compile(agent_match or r"(^|/)(pi|pi-rpc)( |$)|pi-coding-agent")

    def is_agent(self, p):
        c = p.get("cmd") or ""
        if c and self.agent_re.search(c):
            # exclude shells that merely mention the agent in their command line
            return base(p.get("last_exec") or c.split(" ")[0]) not in SHELLS | {"tmux", "env", "timeout", "strace"}
        return False

    def classify(self, procs):
        roles = {}
        agents = {pid for pid, p in procs.items() if self.is_agent(p)}

        def ancestors(pid, limit=40):
            seen = set()
            q = procs.get(pid)
            while q and limit and q.get("ppid") in procs and q["ppid"] not in seen:
                seen.add(q["ppid"])
                q = procs[q["ppid"]]
                limit -= 1
                yield q

        for pid, p in procs.items():
            comm = p.get("comm") or ""
            first = base(p.get("first_exec") or "")
            last = base(p.get("last_exec") or comm)
            anc = list(ancestors(pid))
            anc_agents = [a for a in anc if a["pid"] in agents]
            runc_only = (comm.startswith("runc") or comm == "6") and (not last or last.startswith("runc") or last == "6")
            if pid in agents:
                roles[pid] = "agent:nested" if anc_agents else "agent"
            elif runc_only and not anc_agents:
                roles[pid] = "lab:docker-exec"
            elif comm in ("docker-init", "tini") or (p.get("nspid") == 1) or (last == "sleep" and "infinity" in (p.get("cmd") or "") and not anc_agents):
                roles[pid] = "container:init"
            elif anc_agents:
                parent = procs.get(p.get("ppid"))
                via_sudo = any(a.get("comm") == "sudo" or base(a.get("last_exec")) == "sudo" for a in anc[: anc.index(anc_agents[0])])
                if last in SEARCH or comm in SEARCH:
                    roles[pid] = "tool:search"
                elif comm == "sudo" or last == "sudo":
                    roles[pid] = "tool:sudo"
                elif via_sudo and p.get("uid") == 0:
                    roles[pid] = "tool:root"
                elif parent and parent["pid"] in agents and first not in SHELLS and last not in SHELLS:
                    roles[pid] = "agent:helper"
                elif last in SHELLS or comm in SHELLS:
                    roles[pid] = "tool:shell"
                else:
                    roles[pid] = "tool:command"
            elif comm == "tmux" or last == "tmux" or comm.startswith("tmux") or any((a.get("comm") or "").startswith("tmux") for a in anc):
                roles[pid] = "tui:tmux"  # tmux server/clients and the pane shell that launches the agent
            else:
                # not under an agent: something the lab runner started (docker exec ...)
                roles[pid] = "lab:harness" if (anc or p.get("ppid") is not None) else f"unclassified:{comm}"
        return roles


def label(role, comm=None):
    if role.startswith("unclassified:"):
        return role
    name = ROLE_INFO.get(role, (role, ""))[0]
    if role in ("tool:command", "tool:root", "agent:helper", "lab:harness") and comm:
        return f"{name}: {comm}"
    return name

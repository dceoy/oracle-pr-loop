"""Cross-platform bounded subprocess execution."""
from __future__ import annotations
import os, shutil, signal, subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence
MAX_OUTPUT = 24 * 1024 * 1024
@dataclass(frozen=True)
class CommandResult:
    args: tuple[str, ...]; returncode: int; stdout: bytes; stderr: str
class CommandError(RuntimeError): pass
class CommandRunner:
    def __init__(self, source_env: Mapping[str, str] | None = None) -> None:
        self.source_env = dict(source_env or os.environ)
        self.secrets = {v for k,v in self.source_env.items() if v and len(v)>=4 and any(x in k.upper() for x in ("TOKEN","SECRET","PASSWORD","API_KEY","CREDENTIAL"))}
    def redact(self,text:str)->str:
        for secret in sorted(self.secrets,key=len,reverse=True): text=text.replace(secret,"[REDACTED]")
        return text
    def contains_secret(self,value:str|bytes)->bool:
        return any((s.encode() in value) if isinstance(value,bytes) else (s in value) for s in self.secrets)
    def trusted(self,name:str)->str:
        if os.path.isabs(name): return name
        paths=[p for p in self.source_env.get("PATH","").split(os.pathsep) if os.path.isabs(p)]
        found=shutil.which(name,path=os.pathsep.join(paths))
        if not found: raise CommandError(f"required executable not found: {name}")
        return str(Path(found).resolve())
    def base_env(self)->dict[str,str]:
        env=dict(self.source_env); env.pop("GH_REVIEW_TOKEN",None); return env
    def allowlisted_env(self,extra:set[str]|None=None)->dict[str,str]:
        allowed={"PATH","HOME","USER","LOGNAME","SHELL","TMPDIR","TMP","TEMP","LANG","LANGUAGE","TERM","NO_COLOR","TZ","SSL_CERT_FILE","SSL_CERT_DIR"}|(extra or set())
        return {k:v for k,v in self.base_env().items() if k.upper() in allowed or k.upper().startswith("LC_")}
    def gh_env(self,reviewer_token:str|None=None)->dict[str,str]:
        env=self.allowlisted_env({"GH_TOKEN","GITHUB_TOKEN","GH_CONFIG_DIR","HTTP_PROXY","HTTPS_PROXY","ALL_PROXY","NO_PROXY"})
        if reviewer_token is not None:
            env.pop("GITHUB_TOKEN",None); env["GH_TOKEN"]=reviewer_token; self.secrets.add(reviewer_token)
        return env
    def oracle_env(self)->dict[str,str]:
        return self.allowlisted_env({"CHROME_PATH","DISPLAY","WAYLAND_DISPLAY","XAUTHORITY","DBUS_SESSION_BUS_ADDRESS","ORACLE_BROWSER_PROFILE_DIR","ORACLE_CHATGPT_ACCOUNT_EMAIL"})
    def run(self,args:Sequence[str],*,cwd:Path,env:Mapping[str,str],timeout:int=120,input_text:str|None=None,check:bool=True,max_output:int=MAX_OUTPUT)->CommandResult:
        argv=tuple(str(x) for x in args)
        if not argv or any("\0" in x for x in argv): raise CommandError("invalid subprocess argument vector")
        argv=(self.trusted(argv[0]),*argv[1:])
        proc=subprocess.Popen(argv,cwd=cwd,env=dict(env),stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True,shell=False)
        try: stdout,stderr=proc.communicate(input_text.encode() if input_text is not None else None,timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            try: os.killpg(proc.pid,signal.SIGTERM); proc.wait(timeout=2)
            except (OSError,subprocess.TimeoutExpired):
                try: os.killpg(proc.pid,signal.SIGKILL)
                except OSError: pass
            proc.communicate(); raise CommandError(f"command timed out after {timeout}s: {self.redact(' '.join(argv))}") from exc
        if len(stdout)>max_output or len(stderr)>min(max_output,1024*1024): raise CommandError(f"command output exceeded bound: {self.redact(' '.join(argv))}")
        result=CommandResult(argv,proc.returncode,stdout,self.redact(stderr.decode("utf-8","replace")))
        if check and proc.returncode!=0:
            detail=result.stderr.strip() or self.redact(stdout.decode("utf-8","replace")).strip()
            raise CommandError(f"command failed ({proc.returncode}): {self.redact(' '.join(argv))}: {detail[:2000]}")
        return result

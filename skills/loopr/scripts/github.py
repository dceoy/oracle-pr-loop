"""GitHub and immutable Git snapshot access."""
from __future__ import annotations
import json,re,urllib.parse
from pathlib import Path,PurePosixPath
from typing import Any
from models import EXIT_GITHUB,EXIT_PRECONDITION,EXIT_RACE,LooprError,PullRequest
from process import CommandError,CommandRunner
SHA_RE=re.compile(r"[0-9a-f]{40}\Z"); PART_RE=re.compile(r"[A-Za-z0-9_.-]+\Z")
PR_FIELDS="url,number,title,body,author,state,isDraft,baseRefName,baseRefOid,headRefName,headRefOid,headRepository,headRepositoryOwner,files,changedFiles"
def normalize_repo(remote:str)->str:
 value=remote.strip(); m=re.fullmatch(r"git@github\.com:([^/]+)/([^/]+?)(?:\.git)?",value)
 if m: owner,name=m.groups()
 else:
  p=urllib.parse.urlparse(value)
  if p.scheme not in {"https","ssh"} or p.hostname!="github.com" or p.query or p.fragment: raise LooprError(EXIT_PRECONDITION,"repository","origin must be an unambiguous github.com URL")
  parts=[x for x in p.path.split("/") if x]
  if len(parts)!=2: raise LooprError(EXIT_PRECONDITION,"repository","origin must identify exactly one repository")
  owner,name=parts; name=name.removesuffix(".git")
 if not PART_RE.fullmatch(owner) or not PART_RE.fullmatch(name): raise LooprError(EXIT_PRECONDITION,"repository","invalid repository name")
 return f"{owner}/{name}"
def resolve_target(value:str,origin_repo:str|None)->tuple[str,int,str]:
 if value.isdecimal():
  if origin_repo is None: raise LooprError(EXIT_PRECONDITION,"input","numeric --pr requires an unambiguous local origin")
  repo,number=origin_repo,int(value)
 else:
  p=urllib.parse.urlparse(value); parts=[x for x in p.path.split("/") if x]
  if p.scheme!="https" or p.netloc.lower()!="github.com" or p.query or p.fragment or len(parts)!=4 or parts[2]!="pull" or not parts[3].isdecimal(): raise LooprError(EXIT_PRECONDITION,"input","--pr must be a positive number or canonical GitHub pull URL")
  repo,number=f"{parts[0]}/{parts[1]}",int(parts[3])
 if number<=0: raise LooprError(EXIT_PRECONDITION,"input","pull request number must be positive")
 return repo,number,f"https://github.com/{repo}/pull/{number}"
def validate_ref(ref:str)->None:
 if not ref or ref.startswith(("-",".")) or ref.endswith((".","/",".lock")) or ".." in ref or "@{" in ref or any(ord(c)<32 or ord(c)==127 or c in " ~^:?*[\\" for c in ref): raise LooprError(EXIT_PRECONDITION,"ref","unsafe Git ref")
def validate_path(path:str)->str:
 if not path or "\\" in path or "\0" in path or any(ord(c)<32 or ord(c)==127 for c in path): raise LooprError(EXIT_PRECONDITION,"path","invalid changed path")
 pure=PurePosixPath(path)
 if pure.is_absolute() or ".." in pure.parts or any(p.casefold()==".git" for p in pure.parts): raise LooprError(EXIT_PRECONDITION,"path",f"unsafe changed path: {path}")
 return path
class GitHubClient:
 def __init__(self,runner:CommandRunner,repo_dir:Path,reviewer_token:str)->None:
  self.runner=runner; self.repo_dir=repo_dir.resolve(); self.reviewer_token=reviewer_token; self.repository=""; self.number=0; self.url=""; self.reviewer_login=""
 def _text(self,args:list[str],*,reviewer:bool=False,input_text:str|None=None,max_output:int=24*1024*1024)->str:
  try: r=self.runner.run(["gh",*args],cwd=self.repo_dir,env=self.runner.gh_env(self.reviewer_token if reviewer else None),input_text=input_text,max_output=max_output)
  except CommandError as exc: raise LooprError(EXIT_GITHUB,"github",str(exc)) from exc
  return r.stdout.decode("utf-8","strict")
 def initialize(self,pr_value:str)->None:
  origin_repo=None
  try:
   root=self.runner.run(["git","rev-parse","--show-toplevel"],cwd=self.repo_dir,env=self.runner.base_env()).stdout.decode().strip(); self.repo_dir=Path(root).resolve()
   origin=self.runner.run(["git","remote","get-url","origin"],cwd=self.repo_dir,env=self.runner.base_env()).stdout.decode().strip(); origin_repo=normalize_repo(origin)
  except (CommandError,UnicodeError):
   if pr_value.isdecimal(): raise LooprError(EXIT_PRECONDITION,"repository","cannot infer repository from local checkout") from None
  self.repository,self.number,self.url=resolve_target(pr_value,origin_repo)
  if origin_repo and origin_repo.lower()!=self.repository.lower(): raise LooprError(EXIT_PRECONDITION,"repository","local origin does not match pull request repository")
  if not self.reviewer_token: raise LooprError(EXIT_PRECONDITION,"credentials","GH_REVIEW_TOKEN is required")
  self.reviewer_login=self._text(["api","--hostname","github.com","user","--jq",".login"],reviewer=True).strip()
 def snapshot(self)->PullRequest:
  data=json.loads(self._text(["pr","view",self.url,"--json",PR_FIELDS],max_output=8*1024*1024)); files=data.get("files") or []
  paths=tuple(sorted(validate_path(str(x.get("path") or "")) for x in files))
  if len(paths)!=len(set(paths)): raise LooprError(EXIT_PRECONDITION,"path","duplicate changed paths")
  hr:dict[str,Any]=data.get("headRepository") or {}; ho:dict[str,Any]=data.get("headRepositoryOwner") or {}; head_repo=hr.get("nameWithOwner") or f"{ho.get('login','')}/{hr.get('name','')}"
  pr=PullRequest(self.repository,int(data.get("number",0)),str(data.get("url","")),str(data.get("title","")),str(data.get("body") or ""),str((data.get("author") or {}).get("login") or ""),str(data.get("state") or ""),bool(data.get("isDraft")),str(data.get("baseRefName") or ""),str(data.get("baseRefOid") or ""),str(data.get("headRefName") or ""),str(data.get("headRefOid") or ""),str(head_repo),paths,data)
  if pr.number!=self.number or pr.url.rstrip("/").lower()!=self.url.lower(): raise LooprError(EXIT_PRECONDITION,"identity","ambiguous pull request identity")
  if pr.state!="OPEN" or pr.is_draft: raise LooprError(EXIT_PRECONDITION,"state","pull request must be open and non-draft")
  if pr.head_repository.lower()!=self.repository.lower(): raise LooprError(EXIT_PRECONDITION,"repository","fork pull requests are not supported")
  if not pr.author or pr.author.lower()==self.reviewer_login.lower(): raise LooprError(EXIT_PRECONDITION,"identity","self-review is forbidden")
  if not SHA_RE.fullmatch(pr.base_sha) or not SHA_RE.fullmatch(pr.head_sha): raise LooprError(EXIT_PRECONDITION,"sha","invalid base or head SHA")
  validate_ref(pr.base_ref); validate_ref(pr.head_ref); return pr
 def git_bytes(self,args:list[str],*,max_output:int)->bytes:
  try:return self.runner.run(["git",*args],cwd=self.repo_dir,env=self.runner.base_env(),max_output=max_output).stdout
  except CommandError as exc: raise LooprError(EXIT_PRECONDITION,"git",str(exc)) from exc
 def ensure_objects(self,pr:PullRequest)->None:
  for sha in (pr.base_sha,pr.head_sha):
   if self.git_bytes(["cat-file","-t",sha],max_output=1024).decode().strip()!="commit": raise LooprError(EXIT_PRECONDITION,"git",f"{sha} is not a commit object")
 def changed_file_bytes(self,pr:PullRequest,path:str,*,max_output:int)->bytes:return self.git_bytes(["show",f"{pr.head_sha}:{path}"],max_output=max_output)
 def patch(self,pr:PullRequest,*,max_output:int)->bytes:return self.git_bytes(["diff","--full-index","--find-renames",f"{pr.base_sha}...{pr.head_sha}"],max_output=max_output)
 def tracked_paths(self,pr:PullRequest)->tuple[str,...]:return tuple(sorted(validate_path(x) for x in self.git_bytes(["ls-tree","-r","--name-only",pr.head_sha],max_output=4*1024*1024).decode().splitlines() if x))
 def post_review(self,pr:PullRequest,verdict:str,body:str)->tuple[int,dict[str,Any]]:
  data=json.loads(self._text(["api","--hostname","github.com",f"repos/{pr.repository}/pulls/{pr.number}/reviews","--method","POST","--input","-"],reviewer=True,input_text=json.dumps({"commit_id":pr.head_sha,"body":body,"event":verdict})))
  rid=int(data.get("id",0))
  if rid<=0 or data.get("commit_id")!=pr.head_sha:
   if rid>0:self.dismiss(pr,rid)
   raise LooprError(EXIT_RACE,"race","GitHub did not anchor the review to the expected head")
  return rid,data
 def dismiss(self,pr:PullRequest,rid:int)->None:self._text(["api","--hostname","github.com",f"repos/{pr.repository}/pulls/{pr.number}/reviews/{rid}/dismissals","--method","PUT","--input","-"],reviewer=True,input_text=json.dumps({"message":"Dismissed automatically: reviewed PR snapshot became stale."}))
 def verify_posted(self,pr:PullRequest,rid:int)->dict[str,Any]:
  data=json.loads(self._text(["api","--hostname","github.com",f"repos/{pr.repository}/pulls/{pr.number}/reviews/{rid}"],reviewer=True)); user=data.get("user") or {}
  if int(data.get("id",0))!=rid or user.get("login","").lower()!=self.reviewer_login.lower() or data.get("commit_id")!=pr.head_sha: raise LooprError(EXIT_GITHUB,"github","posted review revalidation failed")
  return data
 @staticmethod
 def same_snapshot(a:PullRequest,b:PullRequest)->bool:return a.base_sha==b.base_sha and a.head_sha==b.head_sha

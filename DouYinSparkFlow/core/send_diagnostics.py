"""Bounded, metadata-only diagnostics, never evidence of business acceptance.

No request interception/body reads, browser operations, send retries or tasks.
"""
from collections import Counter
import hashlib
import re
import os
import time
import threading
from types import SimpleNamespace
from pathlib import Path
from urllib.parse import urlsplit

_PROC_ROOT='/proc'

_LIMIT=24
_PATHS={
    ('imapi.douyin.com','/v1/message/send'):'send',
    ('imapi.douyin.com','/v1/message/get_user_message'):'history',
    ('creator.douyin.com','/passport/safe/get_identity_security_token'):'identity',
    ('creator.douyin.com','/aweme/v1/im/consistency/action/report'):'consistency',
}


def _error_signature(value):
    text=str(value or '')[:4096]
    result={'sha256':hashlib.sha256(text.encode('utf-8',errors='replace')).hexdigest()}
    for name in ['TypeError','ReferenceError','SecurityError','RangeError','SyntaxError','NetworkError']:
        if name in text:result['error_type']=name;break
    match=re.search(r"reading ['\"]([A-Za-z_$][A-Za-z0-9_$]{0,47})['\"]",text)
    if match:result['property']=match.group(1)
    result['mentions_graphics']=bool(re.search('webgl|canvas|gpu|shader|renderer|getParameter',text,re.I))
    result['mentions_crypto']=bool(re.search('crypto|subtle|signature|signing',text,re.I))
    hints=[
        ('sdk_not_initialized', r'IM SDK not initialized'),
        ('sdk_send_rejected', r'IM_SEND_REJECTED'),
        ('conversation_not_found', r'conversation.*not exist in local|ConversationNotExist'),
        ('ticket_error', r'InvalidTicket|ticket.*(?:failed|error)'),
        ('identity_token_error', r'IdentitySecurityToken|identity_security_token'),
    ]
    result['failure_hint']=next((name for name,pattern in hints if re.search(pattern,text,re.I)), 'unclassified')
    # No raw messages, argument values, URLs, chat text or stack contents.
    return result


class _PassiveDiagnostics:
    def __init__(self,page):
        self._page=page;self._cdp=None;self._console_cdp=False
        self._listeners=[];self._closed=False;self._events=[];self._counts=Counter()
        self._started=time.monotonic()
        self._errors={'attach_errors':[],'callback_errors':[],'cleanup_errors':[]}
        self._resources_active=False
        try:
            _RESOURCES.acquire();self._resources_active=True
        except Exception as exc:self._error('attach_errors',exc)
        try:context=page.context
        except Exception as exc:
            self._error('attach_errors',exc);context=None
        self._listen(context,'request',self._request)
        self._listen(context,'response',self._response)
        self._listen(context,'requestfailed',self._failed)
        self._listen(page,'pageerror',self._pageerror)
        self._listen(page,'console',self._console)
        self._listen(page,'crash',lambda *_:self._record({'kind':'page_crash'}))
        self._listen(page,'close',lambda *_:self.close())

    def _error(self,kind,error):
        bucket=self._errors[kind]
        if len(bucket)<8:bucket.append(type(error).__name__)

    async def enable_cdp_console(self):
        """Before initial navigation only: legacy Console works when Runtime is suppressed.

        No injected bindings/wrappers or extra website requests. The CDP session
        lives with the existing page and is disposed by normal browser cleanup.
        """
        if self._closed or self._cdp is not None:return
        try:
            session=await self._page.context.new_cdp_session(self._page)
            if self._closed:return
            self._cdp=session
            self._listen(session,'Console.messageAdded',self._cdp_message)
            self._console_cdp=True
            await session.send('Console.enable')
        except Exception as exc:
            self._console_cdp=False
            self._error('attach_errors',exc)

    def _cdp_message(self,params):
        message=params.get('message') or {}
        self._console(SimpleNamespace(type=message.get('level',''),text=message.get('text',''),
            location={'url':message.get('url',''),'lineNumber':message.get('line'),'columnNumber':message.get('column')}),from_cdp=True)

    def _listen(self,owner,event,callback):
        if owner is None:return
        def safe_callback(*args):
            if self._closed:return
            try:callback(*args)
            except Exception as exc:self._error('callback_errors',exc)
        try:
            owner.on(event,safe_callback)
            self._listeners.append((owner,event,safe_callback))
        except Exception as exc:self._error('attach_errors',exc)

    def _record(self,event):
        event['elapsed_ms']=round((time.monotonic()-self._started)*1000)
        self._events.append(event)
        del self._events[:-_LIMIT]

    def _request_meta(self,request):
        url=urlsplit(str(request.url or ''));path=url.path.rstrip('/')
        kind=_PATHS.get((url.hostname,path))
        if not kind:return None
        method=str(request.method or '').upper()
        return {'call':kind,'path':path,'method':method,'resource_type':str(request.resource_type or '')[:32]}

    def _request(self,request):
        meta=self._request_meta(request)
        if meta is None:return
        if meta['call']=='send' and meta['method']=='POST':self._counts['context_send_requests']+=1
        if meta['call']=='consistency' and meta['method']=='POST':self._counts['context_consistency_requests']+=1
        self._record(dict(meta,kind='context_request'))

    def _response(self,response):
        meta=self._request_meta(response.request)
        if meta is None:return
        if meta['call']=='send' and meta['method']=='POST':self._counts['context_send_responses']+=1
        if meta['call']=='consistency' and meta['method']=='POST':self._counts['context_consistency_responses']+=1
        self._record(dict(meta,kind='context_response',http_status=int(response.status)))

    def _failed(self,request):
        meta=self._request_meta(request)
        if meta is None:return
        if meta['call']=='send' and meta['method']=='POST':self._counts['context_send_failures']+=1
        if meta['call']=='consistency' and meta['method']=='POST':self._counts['context_consistency_failures']+=1
        error=str(request.failure or '')
        match=re.search(r'net::(ERR_[A-Z_]{1,80})',error)
        self._record(dict(meta,kind='context_request_failed',network_error=match.group(1) if match else 'unclassified'))

    def _pageerror(self,error):
        self._counts['page_errors']+=1
        name=str(getattr(error,'name','') or '')
        message=str(getattr(error,'message',error))
        self._record(dict(_error_signature(name+': '+message),kind='page_error'))

    def _console(self,message,*,from_cdp=False):
        if self._console_cdp and not from_cdp:return
        level=str(message.type);text=str(message.text or '')[:4096]
        is_warning=level=='warning' and bool(re.search(r'send message error|IM_SEND_REJECTED|IM SDK not initialized|IdentitySecurityToken|action-consistency.*error',text,re.I))
        if level!='error' and not is_warning:return
        kind='sdk_warning' if is_warning else 'console_error'
        self._counts['sdk_warnings' if is_warning else 'console_errors']+=1
        event={'kind':kind}
        if is_warning:
            # Explicit flags only: no warning digest, property or raw argument.
            for key,pattern in [
                ('sdk_not_initialized',r'IM SDK not initialized'),
                ('sdk_send_rejected',r'IM_SEND_REJECTED'),
                ('conversation_not_found',r'conversation.*not exist in local|ConversationNotExist'),
                ('ticket_error',r'InvalidTicket|ticket.*(?:failed|error)'),
                ('identity_token_error',r'IdentitySecurityToken|identity_security_token'),
                ('mentions_graphics',r'webgl|canvas|gpu|shader|renderer|getParameter'),
            ]:
                event[key]=bool(re.search(pattern,text,re.I))
        else:
            event.update(_error_signature(text))
        location=getattr(message,'location',{}) or {}
        parts=urlsplit(str(location.get('url') or ''))
        file=parts.path.rsplit('/',1)[-1]
        host=parts.hostname or ''
        if (host.endswith('.douyinstatic.com') or host.endswith('.bytetos.com') or host.endswith('.bytegoofy.com')) and re.fullmatch(r'[A-Za-z0-9_.-]{1,100}\.js',file):
            event['source_file']=file
            for source,target in [('lineNumber','source_line'),('columnNumber','source_column')]:
                value=location.get(source)
                if type(value) is int and 0<=value<=10000000:event[target]=value
        self._record(event)

    def snapshot(self):
        # Independent lists/dicts: closing a page cannot mutate a returned record.
        return {'schema':1,'diagnostic_only':True,
                **{k:self._counts[k] for k in ['context_send_requests','context_send_responses','context_send_failures','context_consistency_requests','context_consistency_responses','context_consistency_failures','page_errors','console_errors','sdk_warnings']},
                'events':[dict(x) for x in self._events],
                **{k:list(v) for k,v in self._errors.items()}}

    def close(self):
        if self._closed:return
        self._closed=True
        if self._resources_active:
            self._resources_active=False
            try:_RESOURCES.release()
            except Exception as exc:self._error('cleanup_errors',exc)
        listeners,self._listeners=self._listeners,[]
        for owner,event,callback in listeners:
            try:owner.remove_listener(event,callback)
            except Exception as exc:self._error('cleanup_errors',exc)


def local_resource_snapshot(*, include_browser=False):
    """Read local proc metadata only; no processes, samples or timers started."""
    value={'monotonic':time.monotonic()}
    root=Path(_PROC_ROOT)
    try:
        cpu=[int(x) for x in (root/'stat').read_text().splitlines()[0].split()[1:]]
        # guest/guest_nice are included in user/nice: do not count them twice.
        value.update(cpu_total=sum(cpu[:8]),cpu_idle=cpu[3]+cpu[4])
        mem={line.split(':')[0]:int(line.split()[1]) for line in (root/'meminfo').read_text().splitlines() if len(line.split())>=2}
        value.update(mem_total_kib=mem['MemTotal'],mem_available_kib=mem['MemAvailable'],load1=float((root/'loadavg').read_text().split()[0]))
        if include_browser:
            count=0;rss=0;page_kib=os.sysconf('SC_PAGE_SIZE')//1024
            for index,p in enumerate(root.iterdir()):
                if index>=4096:break
                if not p.name.isdecimal():continue
                try:
                    comm=(p/'comm').read_text().strip().lower()
                    if not any(x in comm for x in ['chrome','chromium','headless_shell']):continue
                    data=(p/'stat').read_text().rsplit(')',1)[1].split()
                    if data[0]=='Z':continue
                    count+=1;rss+=int(data[21])*page_kib
                except (OSError,ValueError,IndexError):continue
            value.update(browser_processes=count,browser_rss_sum_kib=rss)
    except (OSError,ValueError,KeyError,IndexError):value['unavailable']=True
    return value


class _PassiveResources:
    """One daemon per process; proc I/O never runs in the browser event loop.

    Only sample while an existing diagnostic attachment is active. No joins,
    awaitables or file reads are needed to obtain the immutable latest sample.
    """
    def __init__(self):
        self._users=0;self._thread=None;self._latest=None
        self._lock=threading.Lock();self._active=threading.Event()

    def acquire(self):
        with self._lock:
            self._users+=1
            if self._thread is None:
                try:
                    self._thread=threading.Thread(target=self._run,name='sparkflow-passive-resources',daemon=True)
                    self._thread.start()
                except Exception:
                    self._thread=None;self._users-=1
                    raise
            self._active.set()

    def release(self):
        with self._lock:
            self._users=max(0,self._users-1)
            if not self._users:self._active.clear()

    def _run(self):
        while True:
            self._active.wait()
            try:
                value=local_resource_snapshot(include_browser=True)
                if self._users:self._latest=value
            except Exception:
                pass
            time.sleep(2)

    def peek(self):
        value=self._latest
        if value is None or time.monotonic()-value['monotonic']>10:return None
        return dict(value)


_RESOURCES=_PassiveResources()


def cached_local_resources():
    return _RESOURCES.peek()


def resource_interval(before,after):
    result={'diagnostic_only':True,'interval_seconds':round(max(0,after['monotonic']-before['monotonic']),3)}
    for key in ['mem_available_kib','mem_total_kib','load1','browser_processes','browser_rss_sum_kib','unavailable']:
        if key in after:result[key]=after[key]
    total=after.get('cpu_total',0)-before.get('cpu_total',0)
    idle=after.get('cpu_idle',0)-before.get('cpu_idle',0)
    if 'cpu_total' in before and 'cpu_total' in after and total>0 and 0<=idle<=total:
        result['host_cpu_percent']=round(100*(total-idle)/total,1)
    if after.get('mem_total_kib',0)>0:
        result['mem_available_percent']=round(100*after['mem_available_kib']/after['mem_total_kib'],1)
    return result


VISIBLE_UI_FEEDBACK_JS=r'''() => {
        const result={visible_regions:0,send_failed:false,verification:false,login:false,busy:false,restriction:false};
        const nodes=document.querySelectorAll('.semi-toast-content,.semi-notification-content,[role="alert"],[role="dialog"],.semi-modal-content');
        for (const node of Array.from(nodes).slice(0,64)) {
            if(node.closest('[contenteditable="true"],[class*="box-item-"]'))continue;
            const r=node.getBoundingClientRect(),s=getComputedStyle(node);
            if(!r.width||!r.height||s.display==='none'||s.visibility==='hidden')continue;
            result.visible_regions++;
            const text=String(node.innerText||'').slice(0,2000);
            result.send_failed ||= /发送失败|消息发送失败|send.*failed/i.test(text);
            result.verification ||= /验证码|安全验证|身份验证|captcha/i.test(text);
            result.login ||= /重新登录|请登录|扫码登录/.test(text);
            result.busy ||= /系统繁忙|稍后再试|操作频繁/.test(text);
            result.restriction ||= /限制|不可发送|无法发送|禁止发送/.test(text);
        }
        return result;
    }'''


async def visible_ui_feedback(page):
    """Read fixed flags from visible system regions, not chat contents."""
    return await page.evaluate(VISIBLE_UI_FEEDBACK_JS)


def attach_passive_send_diagnostics(page):
    """Attach read-only context/page event listeners for one existing attempt."""
    return _PassiveDiagnostics(page)

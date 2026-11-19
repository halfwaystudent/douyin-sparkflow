import pytest
from core import browser_resources as br

M=1024**2

def sample(available=512*M,cgroups=None):
    return {'host':{'mem_total_bytes':2048*M,'mem_available_bytes':available,'read_error':None},'cgroups':cgroups or [],'platform':'linux'}

def test_namespaced_cgroup_root_is_resolved_from_mountinfo(monkeypatch):
    values={
        '/proc/meminfo':'MemTotal: 2048000 kB\nMemAvailable: 1572864 kB\n',
        '/proc/self/cgroup':'4:memory:/docker/fixture\n',
        '/proc/self/mountinfo':'31 23 0:27 /docker/fixture /sys/fs/cgroup/memory rw - cgroup cgroup rw,memory\n',
        '/sys/fs/cgroup/memory/memory.limit_in_bytes':str(512*M),
        '/sys/fs/cgroup/memory/memory.usage_in_bytes':str(500*M),
    }
    monkeypatch.setattr(br,'_read_text',lambda p:values.get(str(p)))
    with pytest.raises(br.BrowserResourceUnavailable):br.ensure_browser_resources()


def test_unreadable_finite_limit_is_not_unlimited(monkeypatch):
    values={'/proc/meminfo':'MemTotal: 2048000 kB\nMemAvailable: 1572864 kB\n',
            '/proc/self/cgroup':'0::/\n','/sys/fs/cgroup/memory.max':'broken',
            '/sys/fs/cgroup/memory.current':'1000'}
    monkeypatch.setattr(br,'_read_text',lambda p:values.get(str(p)))
    with pytest.raises(br.BrowserResourceUnavailable):br.ensure_browser_resources()


def test_low_host_cannot_be_hidden_by_roomy_cgroup():
    data=sample(20*M,[{'limit_bytes':1024*M,'current_bytes':100*M,'read_error':None}])
    with pytest.raises(br.BrowserResourceUnavailable):br.ensure_browser_resources(sampler=lambda:data)

def test_missing_cgroup_usage_is_not_zero_usage():
    data=sample(cgroups=[{'limit_bytes':1024*M,'current_bytes':None,'read_error':'unreadable'}])
    with pytest.raises(br.BrowserResourceUnavailable):br.ensure_browser_resources(sampler=lambda:data)

def test_linux_missing_telemetry_refuses_launch():
    data=sample();data['host']={'read_error':'meminfo_unreadable'}
    with pytest.raises(br.BrowserResourceUnavailable):br.ensure_browser_resources(sampler=lambda:data)

def test_unsupported_platform_does_not_require_linux_telemetry():
    data={'host':{},'cgroups':[],'platform':'darwin'}
    assert br.ensure_browser_resources(sampler=lambda:data)['measured'] is False

def test_zero_available_is_still_measured(monkeypatch):
    def read(path):
        if str(path)=='/proc/meminfo':return 'MemTotal: 2048000 kB\nMemAvailable: 0 kB\n'
        if str(path)=='/proc/self/cgroup':return ''
        return None
    monkeypatch.setattr(br,'_read_text',read)
    with pytest.raises(br.BrowserResourceUnavailable):br.ensure_browser_resources()

def test_negative_env_cannot_disable_guard(monkeypatch):
    monkeypatch.setenv('SPARKFLOW_BROWSER_LAUNCH_MIN_AVAILABLE_MB','-1')
    monkeypatch.setenv('SPARKFLOW_BROWSER_LAUNCH_MIN_AVAILABLE_RATIO','-1')
    with pytest.raises(br.BrowserResourceUnavailable):br.ensure_browser_resources(sampler=lambda:sample(10*M))

def test_nan_ratio_cannot_crash_the_resource_classifier(monkeypatch):
    monkeypatch.setenv('SPARKFLOW_BROWSER_LAUNCH_MIN_AVAILABLE_RATIO','nan')
    with pytest.raises(br.BrowserResourceUnavailable):br.ensure_browser_resources(sampler=lambda:sample(10*M))

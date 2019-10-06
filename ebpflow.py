#!/usr/bin/env python
import signal
import os
import re
import sys
import json
import time
import threading
import argparse

from socket import inet_ntop, ntohs, AF_INET
from struct import pack

import ctypes as ct
from bcc import BPF


print('ebpflow: -h to show options')

# ----- Globals and Termination ----- #
RUNNING = True

def signal_handler(sig, frame):
	global RUNNING
	RUNNING = False
signal.signal(signal.SIGINT, signal_handler)

# ----- Argument parsing ----- #
dscr = 'TCP flow monitor tool based on eBPF'
parser = argparse.ArgumentParser(description=dscr)
# Task filter
parser.add_argument('-t', '--task', default=None,
  help='filter events of a specific task')
# Container full id
parser.add_argument('--no-trunc', dest='no_trunc', action='store_true',
  help='show docker full id')
args = parser.parse_args()
FILTER_TASK = args.task
NO_TRUNC = args.no_trunc


# *************************************** #
# ===== ===== DATA STRUCTURES ===== ===== #
# *************************************** #
# ----- User Kernel Data Structures ----- #
TASK_COMM_LEN = 16 
CGROUP_NAME = 64
class task_info(ct.Structure):
  _fields_ = [
    ("pid", ct.c_uint32),
    ("uid", ct.c_uint32),
    ("gid", ct.c_uint32),
    ("cgroup", ct.c_char * CGROUP_NAME),
    ("task", ct.c_char * TASK_COMM_LEN)
	]

class net_info4(ct.Structure):
  _fields_ = [
    ("loc_port", ct.c_uint16),
    ("dst_port", ct.c_uint16),
    ("saddr", ct.c_uint32),
    ("daddr", ct.c_uint32),
	]

class kernel_data(ct.Structure):
  """
  Redefine ctypes to handle and nicely print ebpflow events
  """
  _fields_ = [
    ("absolute_time", ct.c_uint64),
    ("ktime", ct.c_uint64),
    ("task", task_info),
    ("ptask", task_info),
    ("etype", ct.c_int),
    ("net4", net_info4)
  ]

  _ltask = '[ktime: %s][gid: %s][uid: %s][pid: %s][%s]'
  _lparent = 'parent: [gid: %s][uid: %s][pid: %s][%s]'
  _lnetinfo = 'netinfo: [%s][IPv4][%s:%s <-> %s:%s]'
  _lcgroup = 'container: [dockerid: %s]'
  
  _etype_table = {
    601: 'TCP/ACC',
    602: 'TCP/CONN'
  }
  def etype2str (self): 
    return self._etype_table[self.etype]

  def __str__ (self):
    lines = []
    lines.append(self._ltask % (self.ktime, self.task.gid, self.task.uid, self.task.pid, self.task.task))
    lines.append(self._lparent % (self.ptask.gid, self.ptask.uid, self.ptask.pid, self.ptask.task))
    lines.append(self._lnetinfo % (self.etype2str(), 
      inet_ntop(AF_INET, pack("I", self.net4.saddr)), self.net4.loc_port, 
      inet_ntop(AF_INET, pack("I", self.net4.daddr)), self.net4.dst_port))
    if self.task.cgroup != '/':
      dockerid = self.task.cgroup if NO_TRUNC else self.task.cgroup[:12]
      lines.append(self._lcgroup % dockerid)
    return '\n|__'.join(lines)


# ----- Events Statistics ----- #
class AtomicInteger():
  def __init__(self, t_value=0):
    self.m_value = t_value
    self.m_lock = threading.Lock()

  def __add__(self, t_v):
    with self.m_lock:
      self.m_value += t_v
      return self

  def get(self):
    with self.m_lock:
      return self.m_value


class Events_Statics():
  """
  Accounts for connection events (etype==602) and accept(etype==601) events
  """
  def __init__(self):
    self.connect_counter = AtomicInteger(0)
    self.accept_counter = AtomicInteger(0)

  def add(self, e):
    if e.etype == 601:
      self.accept_counter += 1
    else:
      self.connect_counter += 1

  def __str__(self):
    line = '===== Events count =====\ntot: %s \naccpt: %s \nconn: %s'
    conn = self.accept_counter.get()
    acpt = self.connect_counter.get()
    return (line % (conn + acpt, conn, acpt))


# ************************************* #
# ===== ===== EVENT HANDLER ===== ===== #
# ************************************* 
estats = Events_Statics()
def print_ipv4_event(cpu, data, size):
  global estats
  event = ct.cast(data, ct.POINTER(kernel_data)).contents
  estats.add(event)
  print(str(event))


# **************************************** #
# ===== ===== ATTACHING PROBES ===== ===== #
# **************************************** #
def readebpf(src, task=None):
  """
  Load ebpf program in a string and, if specified, apply
  a filter on the task's name
  Argument:
    task - the task name whose events we are interested on
  Return: a string representing the eBPF program  
  """
  with open('ebpf.c', 'r') as ebpfile:
    ebpftxt = ebpfile.read()

  fltr = ''
  if task is not None:
    fltr = 'if(ebpf_strcmp(event_data.task.task, "%s") == 0)' % (task)
  ebpftxt = ebpftxt.replace('FLTR_TASK', fltr) 

  return ebpftxt


# ----- Loading and manipulating ebpf ----- #
print('> Starting up...')
ebpf_str = readebpf('ebpf.c', FILTER_TASK)
bpf = BPF(text=ebpf_str)
print('> eBPF code loaded')

# ----- Attaching probes ----- #
bpf.attach_kprobe(event="tcp_v4_connect", fn_name="trace_connect_entry")  
bpf.attach_kretprobe(event="tcp_v4_connect", fn_name="trace_connect_v4_return")
bpf.attach_kretprobe(event="inet_csk_accept", fn_name="trace_accept_return") 
print('> eBPF event attached')

# ----- Opening the buffer ----- #
bpf["user_buffer"].open_perf_buffer(print_ipv4_event)
print('> Output buffer opened')

# ----- Polling events ----- #
print('> Start polling events. CTRL+C to stop\n')
while RUNNING:
  bpf.perf_buffer_poll(timeout=50)

print('\r  \n' + str(estats))

#!/usr/bin/python
#
# CondSrv.py -- Web inteface and high-level controls for miscellaneous apartment
#  housekeping.
#
# See RoomCtl.ino for Arduino sketch.
#
# Assumes Python 2.7.3, currently run on OpenWRT router box
#

import time
import serial
import re
import Image,ImageDraw
import cStringIO
import datetime
import threading
import sys
import glob
import shelve

sys.stderr = sys.stdout

import os, errno

import pdb

from cgi import parse_qs, escape
from wsgiref.simple_server import (make_server, WSGIRequestHandler)

#
# Global configuration parameters
#
CfgAcCtlEnabled = False	# AC control enabled
CfgTemp = 0		# Target room temperature
CfgEvents = []		# Scheduler events

ExtT = []    # List of external temperatures [120*3]
PrevExtT = []   # List of previous days external temperatures [2][120*3]
RoomT = []	# List of room temperatures for current day [120*3]
BaroP = []	# List of baro pressures for current day [120*3]
FridgeT = []	# List of fridge temperatures for current day [120*3]

# Format: "31/Dec/2012 15:31:16"
def DateTime():
 d = datetime.datetime.now()
 return d.strftime("%d/%b/%Y %H:%M:%S");


#
# com = OpenPort('/dev/ttyUSB0', rate);
#
def OpenPort(devName, rate):
 com = serial.Serial(port=devName, baudrate=rate,
         parity=serial.PARITY_NONE,
         stopbits=serial.STOPBITS_ONE,
         bytesize=serial.EIGHTBITS,
         xonxoff=0,
         rtscts=0,
         timeout=2);
 com.open();
 com.isOpen();
 return com;

# Exception class to return
class PortError(Exception):
 def __init__(self, name):
  self.name = name
 def __str__(self):
  return repr(self.name)

#
# Reconnect(PortError x) - try to reconnect to the port, probably at different index
#
def Reconnect(x):
 global comC, comR
 time.sleep(2)
 if re.search(".*ttyUSB.*", x.name):
   (comC, nameX) = FindPort("/dev/ttyUSB*", 38400)
 else:
   (comR, nameX) = FindPort("/dev/ttyACM*", 9600)
 time.sleep(2);
 print("["+DateTime()+"] Error encountered on \""+x.name+"\", reconnected as \""+nameX+"\"");


#
# [] = WaitReply(com [, "Command"])
#
def WaitReply(com, cmd=""):
 global LockC, LockR

 try:
  if com == comC:
   LockC.acquire();
  else:
   if com == comR:
    LockR.acquire();

  r = [];				# Clear list of returned lines
  if cmd:				# Command specified
   com.write(cmd+'\r');

  tmoCnt = 5;
  for i in range(0, 200):
   s = com.readline();
   s = s.strip();
   if s == "":
     tmoCnt = tmoCnt-1
     if tmoCnt == 0:
       raise PortError(com.port)

#   if s and not re.search('^Temp\[*', s):
#    sys.stdout.write("Re:\""+s+"\"\r\n");

   if s and not re.search('AT.*', s):	# Not "AT..." echo returned
    if s == "OK":			# "OK" (end of output)
     return r;				# so return accumulated array
    else:
     r.append(s);

  sys.exit("Too many lines received in reply to command \""+cmd+"\"");

 except OSError, ex:
  if ex.errno != 5:
   sys.exit("Unexpected (not errno 5) OSError in WaitReply:\""+ex.strerror+"\"")
  print "["+DateTime()+"] OSError:5 encountered on \""+com.port+"\""
  raise PortError(com.port)

 finally:
  if com == comC:
   LockC.release();
  else:
   if com == comR:
    LockR.release();

#
# Helper functions for getting various peripheral states
#
def GetCurrentMode(com):
 t=re.search('.*\]=(.+)', WaitReply(com, 'AT*mod$')[0]);
 if t:
  r = 'On' if t.group(1) == '3' else 'Off'
 else:
  r = 'N/A'
 print 'Current Mode:'+r;
 return r;

def GetCurrentHighFanMode(com):
 t=re.search('.*\]=(.+)', WaitReply(com, 'AT*ENH$')[0]);
 if t:
  r = 'Forced' if t.group(1) == '2' else 'On' if t.group(1) == '1' else 'Off'
 else:
  r = 'N/A'
 print 'Current HF Mode:'+r;
 return r;

def GetCurrentLowFanMode(com):
 t=re.search('.*\]=(.+)', WaitReply(com, 'AT*ENL$')[0]);
 if t:
  r = 'Forced' if t.group(1) == '2' else 'On' if t.group(1) == '1' else 'Off'
 else:
  r = 'N/A'
 print 'Current LF Mode:'+r;
 return r;

def GetCurrentBlindsMode(com):
 s = WaitReply(com, 'ATSRVR')[0]
 t=re.search('.*:(.+)', s);
 if t:
  r = t.group(1);
 else:
  r = 'N/A'
 print 'Current Blinds Mode:'+r;
 return r;


# Return current temperatures
# xt,rt,ft = GetTemperatures()
def GetTemperatures():
 global comC
 extT = 0
 roomT = 0
 fridgeT = 0

 for i in range(3):
   try:
     l = WaitReply(comC, "ATTS")
     if re.search('Key', l[0]):
       break
   except PortError, x:
     Reconnect(x)

 for i in range(len(l)):
   m = re.search('30274600000056.*T=(\d*\.\d*)',l[i])   # Room temperature
   if m:
     t = float(m.group(1))
     roomT = t

   m = re.search('AA8E5B02080074.*T=(\d*\.\d*)',l[i])   # Fridge temperature (now just outside)
   if m:
     t = float(m.group(1))
     fridgeT = t

   m = re.search('7C519F0008004B.*T=(\d*\.\d*)',l[i])   # External temperature
   if m:
     t = float(m.group(1))
     extT = t

 print('Current XT:%f'%extT)
 print('Current RT:%f'%roomT)
 print('Current FT:%f'%fridgeT)
 return extT, roomT, fridgeT


def GetBaroP():
 global comR
 BaroP = 0

 s = WaitReply(comR, 'ATBARO')[0]
 t=re.search('.*P:(.+)', s);
 if t:
  BaroP = float(t.group(1));
  BaroP = BaroP * 0.00750061561303	# Convert Pa to mmHg
 else:
  BaroP = 0.

 return BaroP;


def GetRoomTargetT():
 global comC

 t=re.search('.*\]=(.+)', WaitReply(comC, 'AT*TGN$')[0]);
 if t:
  r = int(t.group(1))/10.
 else:
  r = 0.
 print ('Current TT:%f |'+t.group(1))%r
 return r;


# Read external temperature data
def GetExtTempQQ():
 global comC

 Temp_reply = WaitReply(comC, 'ATG');		# Read ext temperature data
 Temp = [];
 for s in Temp_reply:
  t = re.search('.*=(.+)', s)
  if t:
   Temp.append(int(t.group(1)))
 t = re.search('history at (\d+):(\d+):(\d+)\(', Temp_reply[0]);
 h=int(t.group(1));
 m=int(t.group(2));
 s=int(t.group(3));

 return (Temp,h,m,s)


#
# Main html page header
# (with auto-refresh)
#
HdrHtml = """
  <!DOCTYPE html
   PUBLIC "-//WAPFORUM//DTD XHTML Mobile 1.0//EN"
		 "http://www.wapforum.org/DTD/xhtml-mobile10.dtd">
		 <html xmlns="http://www.w3.org/1999/xhtml" lang="en-US" xml:lang="en-US">
		 <head>
		 <title>CondCtl</title>
		 <meta http-equiv="Content-Type" content="text/html; charset=iso-8859-1" />
<meta Http-Equiv="Cache-Control" Content="no-cache">
<meta Http-Equiv="Pragma" Content="no-cache">
<meta Http-Equiv="Expires" Content="0">
<meta Http-Equiv="Pragma-directive: no-cache">
<meta Http-Equiv="Cache-directive: no-cache">
<meta name="viewport" content="width=400">
<meta http-equiv="refresh" content="60; url=/cgi-bin/condctl">
		 <head>
		 <body>
"""

#
# Settings page header
#
SettingsHdrHtml = """
  <!DOCTYPE html
   PUBLIC "-//WAPFORUM//DTD XHTML Mobile 1.0//EN"
		 "http://www.wapforum.org/DTD/xhtml-mobile10.dtd">
		 <html xmlns="http://www.w3.org/1999/xhtml" lang="en-US" xml:lang="en-US">
		 <head>
		 <title>CondCtl</title>
		 <meta http-equiv="Content-Type" content="text/html; charset=iso-8859-1" />
<meta Http-Equiv="Cache-Control" Content="no-cache">
<meta Http-Equiv="Pragma" Content="no-cache">
<meta Http-Equiv="Expires" Content="0">
<meta Http-Equiv="Pragma-directive: no-cache">
<meta Http-Equiv="Cache-directive: no-cache">
<meta name="viewport" content="width=400">
		 <head>
		 <body>
"""

#
# Main control form template
#
MainFormHtmlTemplate = """
<img SRC="/cgi-bin/genimg">
<form>
<table cellpadding=5 cellspacing=5 border=1>
<tr><td>Mode: </td>    <td> %s</td><td><INPUT TYPE=submit NAME='CondCtlMode' VALUE='Off'><INPUT TYPE=submit NAME='CondCtlMode' VALUE='On'></td></tr>
<tr><td>T:%4.1f/%4.1f,%4.1f</td><td>%s</td><td><INPUT TYPE=submit NAME='TargetT' VALUE='-'><INPUT TYPE=submit NAME='TargetT' VALUE='+'></td></tr>
<tr><td>Fan: </td><td> %s</td><td><INPUT TYPE=submit NAME='HighFanMode' VALUE='Off'><INPUT TYPE=submit NAME='HighFanMode' VALUE='On'><INPUT TYPE=submit NAME='HighFanMode' VALUE='Forced'></td></tr>
<tr><A href="/cgi-bin/settings">Settings</A></tr>
<tr><td>Blinds: </td>  <td> %s</td><td><INPUT TYPE=submit NAME='BlindsMode'  VALUE='88'> <INPUT TYPE=submit NAME='BlindsMode' VALUE='90'><INPUT TYPE=submit NAME='BlindsMode' VALUE='95'><INPUT TYPE=submit NAME='BlindsMode' VALUE='100'></td></tr>
</table>
</form>
%s
"""


#
# MasterOff() - Disable all climate control functions
# Inputs:
#
#	port, cmd - Ignored (just for generic command compatibility)
#
def MasterOff(port, cmd):
  global comC, comR, LockCfg
  WaitReply(comC, 'AT*MOD=0')	 # Switch CondCtl off
  with LockCfg:
    if CfgAcCtlEnabled:
       WaitReply(comR, 'ATAC=0') # Switch AC off

#
# MasterOn() - Enable all climate control functions
# Inputs:
#
#	port, cmd - Ignored (just for generic command compatibility)
#
def MasterOn(port, cmd):
  global comC, comR, LockCfg, CfgTemp
  WaitReply(comC, 'AT*MOD=3')	 # Switch CondCtl off
  with LockCfg:
    if CfgAcCtlEnabled:
       WaitReply(comR, 'ATAC=%d'%CfgTemp) # Switch AC on, set temperature


# Event types and mapping of select strings to them
class EvType:
  Off, On, SetT, SetB88, SetB100 = range(0, 5)

Select2evType = { 'Off':EvType.Off, 'On':EvType.On, 'SetT':EvType.SetT, 'SetB88':EvType.SetB88, 'SetB100':EvType.SetB100 }

class SchedEvent:
  evEnabled = False
  evTime = datetime.time(0, 0)
  evDays = 0x7F
  evType = EvType.Off
  evTemp = 0

#  def Execute(self):
#    return
#    if self.evType == EvType.Off:
#      MasterOff(0,'')
#    elif self.evType == EvType.On:
#      MasterOn(0,'')
#    elif self.evType == EvType.SetT:
#      qqqq
#    elif self.evType == EvType.SetB88:
#    elif self.evType == EvType.SetB100:


  def GenerateString(self):
    r = "";
    if self.evEnabled:
      r += '[X]'
    else:
      r += '[ ]'

    r += " %02d:%02d"%(self.evTime.hour, self.evTime.minute)
    r += " %02X "%(self.evDays)

    for (s, t) in (s, t) in Select2evType.iteritems():
       if t == self.evType:
          r += s
          break
    r += " %02d"%self.evTemp
    return r


  def GenerateFormString(self, id):
    r = """<tr>"""
    r += """<td> <input name="EvEn_#id#" type="checkbox"  value="True" %s> Enable</td>"""%("checked" if self.evEnabled else '')+"\n"
    r += """<td> <input name="EvTi_#id#" type="time" value="%02d:%02d"></td>"""%(self.evTime.hour, self.evTime.minute)+"\n"
    r += """<td>"""+"\n"
    t = 1
    for i in range(0, 7):
      r += """<input name="EvD%d_#id#"   type="checkbox"  value="True" %s>"""%(i, "checked" if (self.evDays & t) != 0 else '')+"\n"
      t <<= 1
    r += """</td>"""+"\n"
    r += """<td>"""
    r += """<select name="EvTy_#id#">"""+"\n"

    for s in ['Off','On','SetT','SetB88','SetB100']:	# Use convenient ordering instead of arbitrary dictionary enumeration
      r += """<option %s>%s</option>"""%("selected" if Select2evType[s] == self.evType else '', s)+"\n"
    r += """</select></td>"""+"\n"
    r += """</td>"""+"\n"
    r += """<td>"""+"\n"
    r += """<select name="EvT_#id#">"""+"\n"
    for t in range(16,26):
      r += """<option %s>%d</option>"""%("selected" if self.evType == EvType.SetT and self.evTemp == t else '', t)+"\n"
    r += """</select></td>"""+"\n"
    r += """<td> <button TYPE=submit NAME="Del" value="#id#">Del</button></td>"""+"\n"
    r = r.replace("#id#", str(id))	# Set event index into parameter names
    return r


#
# Main WSGI application handler
#
def application(environ, start_response):
 global comC, comR, LockC, LockR, LockCfg, RoomT, FridgeT, BaroP

 def GenImg():
  global comC, comR

#  (Temp,h,m,s) = GetExtTemp()

  # Generate plot
  sizeX = 120*3;
  sizeY = 200;

  img = Image.new("RGB", (sizeX, sizeY), "#FFFFFF");
  draw = ImageDraw.Draw(img);
  white = (255,255,255);
  black = (0,0,0);
  gridColor = (230,230,230);
  strobeColor = (0,180,0);
  lineColor = (0,0,255);
  lineIColor = (0,200,255);

  draw.rectangle([0, 0, sizeX-1, sizeY-1], outline=black);

  for i in range(1, 24):
   draw.line([i*sizeX/24, 1, i*sizeX/24, sizeY-2], fill=(245,245,245), width=1);

  for i in range(-25, 25+1, 10):
   draw.line([1, sizeY/2-i*3, sizeX-2, sizeY/2-i*3], fill=(245,245,245), width=1);

  for i in range(-30, 30+1, 10):
   draw.line([1, sizeY/2-i*3, sizeX-2, sizeY/2-i*3], fill=gridColor, width=1);

  draw.line([0, sizeY/2, sizeX-1, sizeY/2], fill=black, width=1);

  tim = datetime.datetime.now()
  curPos = (tim.hour*60*60+tim.minute*60+tim.second)*sizeX / (24*60*60)
  curNdx = (tim.hour*60 + tim.minute)/(12/3)

  draw.line([curPos, 1, curPos, sizeY-2], fill=strobeColor, width=1)

  r = 3
  draw.ellipse([curPos-r, sizeY/2-ExtT[curNdx]*3-r, curPos+r, sizeY/2-ExtT[curNdx]*3+r], outline=lineColor)
  draw.ellipse([curPos-r, sizeY/2-((RoomT[curNdx]-20)*2)*3-r, curPos+r, sizeY/2-((RoomT[curNdx]-20)*2)*3+r], outline=lineIColor)

  nSamplesR = 120 * 3

#  for i in range(nSamplesR-1):
#   draw.line([i*sizeX/nSamplesR, sizeY/2-PrevExtT[1][i]*3,
#             (i+1)*sizeX/nSamplesR, sizeY/2-PrevExtT[1][i+1]*3], fill=(190,190,255), width=1);

#  for i in range(nSamplesR-1):
#   draw.line([i*sizeX/nSamplesR, sizeY/2-PrevExtT[0][i]*3,
#             (i+1)*sizeX/nSamplesR, sizeY/2-PrevExtT[0][i+1]*3], fill=(130,130,255), width=1);

  for i in range(nSamplesR-1):
   draw.line([i*sizeX/nSamplesR, sizeY/2-ExtT[i]*3,
             (i+1)*sizeX/nSamplesR, sizeY/2-ExtT[i+1]*3], fill=lineColor, width=1);

  for i in range(nSamplesR):
   draw.point([i*sizeX/nSamplesR, sizeY/2-FridgeT[i]*3], fill=(255,200,255));

  for i in range(nSamplesR):
   draw.point([i*sizeX/nSamplesR, sizeY/2-((RoomT[i]-20)*2)*3], fill=lineIColor);

  for i in range(nSamplesR):
   draw.point([i*sizeX/nSamplesR, sizeY/2-(BaroP[i]-760-10)*3], fill=(224,86,27));


  draw.text([2,2], "%02d:%02d:%02d"%(tim.hour, tim.minute, tim.second), fill=strobeColor);

  f = cStringIO.StringIO()
  img.save(f, "PNG")

  f.seek(0)

  return [f.getvalue()]

#
# SettingsPage() - Handle settings page, setting global configuration parameters
#
 def SettingsPage():
  global CfgAcCtlEnabled, CfgEvents, LockCfg

  # Parse configuration parameters, if any

  params = parse_qs(environ.get('QUERY_STRING', ''))

  if 'Add' in params:		# Add new event
    with LockCfg:
      CfgEvents.append(SchedEvent())

  elif 'Del' in params:		# Delete requested
    i = int(params['Del'][0])
    with LockCfg:
      del CfgEvents[i]


  elif 'Save' in params:	# Save requested
    with LockCfg:
      if 'ControlAC' in params:
        CfgAcCtlEnabled = True
      else:
        CfgAcCtlEnabled = False

      for i in range(len(CfgEvents)):
        CfgEvents[i].evEnabled = False
        CfgEvents[i].evDays = 0

      for key in params:
        m = re.search('(.*)_(.*)', key)
        if m:			# Match -- "indexed" name, must belong to scheduler event
          name = m.group(1)
          i = int(m.group(2))
          if i >= len(CfgEvents):
            break
          if name == 'EvEn':
            CfgEvents[i].evEnabled = True
          else:
            val = escape(params[key][0])
            if name == 'EvTi':
              m2 = re.search('(.*):(.*)', val)
              if m2:
                CfgEvents[i].evTime = datetime.time(int(m2.group(1)), int(m2.group(2)))
            elif name == 'EvTy':
              if val in Select2evType:
                CfgEvents[i].evType = Select2evType[val]
              else:
                CfgEvents[i].evType = EvType.Off
            elif name == 'EvT':
              for t in range(16,26):
                if str(t) == val:
                  CfgEvents[i].evTemp = t
            else:
              m = re.search('EvD(.*)_(.*)', key)
              if m:
                CfgEvents[i].evDays |= 1 << int(m.group(1))

    # Saving parameters, save active configuration to file
    sh = shelve.open('condsrv',writeback=True)
    sh['CfgAcCtlEnabled'] = CfgAcCtlEnabled
    sh['CfgEvents'] = CfgEvents
    sh.close()


  # Generate HTML page, using current configuration
  r = [SettingsHdrHtml]

  # Generate form
  r.append("""<form>""")
  r.append("""<input type="checkbox" name="ControlAC" value="On" %s> Control AC"""%("checked" if CfgAcCtlEnabled else '')+"\n")
  r.append("""<table cellpadding=2 cellspacing=2 border=1>""")
  for i in range(len(CfgEvents)):
    r.append(CfgEvents[i].GenerateFormString(i))
  r.append("""</table>""")
  r.append("""<INPUT TYPE=submit NAME="Add" value="Add">""")
  r.append("""<INPUT TYPE=submit NAME="Save" value="Save">""")
  r.append("""</form>""")
  return r


 def CondCtl():
  global comC, comR

  def TargetTInc(com, cmd):
   t = GetRoomTargetT()
   t = t + 0.5
   WaitReply(com, 'AT*TGN=%d'%(t*10))


  def TargetTDec(com, cmd):
   t = GetRoomTargetT()
   t = t - 0.5
   WaitReply(com, 'AT*TGN=%d'%(t*10))

  commandMap = [
   ('CondCtlMode', 'On',    MasterOn,   '',	   0),
   ('CondCtlMode', 'Off',   MasterOff,  '',	   0),
   ('TargetT',     '+',     TargetTInc, '',        comC),
   ('TargetT',     '-',     TargetTDec, '',        comC),
   ('HighFanMode', 'On',    WaitReply, 'AT*ENH=1', comC),
   ('HighFanMode', 'Off',   WaitReply, 'AT*ENH=0', comC),
   ('HighFanMode', 'Forced',WaitReply, 'AT*ENH=2', comC),
   ('LowFanMode',  'On',    WaitReply, 'AT*ENL=1', comC),
   ('LowFanMode',  'Off',   WaitReply, 'AT*ENL=0', comC),
   ('LowFanMode',  'Forced',WaitReply, 'AT*ENL=2', comC),
   ('BlindsMode',  '88',    WaitReply, 'ATSRV=88', comR),
   ('BlindsMode',  '90',    WaitReply, 'ATSRV=90', comR),
   ('BlindsMode',  '95',    WaitReply, 'ATSRV=95', comR),
   ('BlindsMode',  '100',   WaitReply, 'ATSRV=100',comR)  ]


  # Do configuration if requested (if parameters are present in query string)
  params = parse_qs(environ.get('QUERY_STRING', ''))
  for t in commandMap:
   (name, value, fnc, cmd, port) = t
   if name in params:
    if value == escape(params[name][0]):
     fnc(port, cmd)

#  if 'LightMode' in form:
#   if form['LightMode'].value == 'Pulse':
#    WaitReply(comR, 'ATIRPM=2000');
#    WaitReply(comR, 'ATDLY=500');
#    WaitReply(comR, 'ATIRPM=200');

  # Read bandwidth monitor log file, include last line from it
  fileHandle = open('/root/ckbw.log',"r")
  lineList = fileHandle.readlines()
  fileHandle.close()
  if len(lineList) >= 1:
   recentRate = lineList[len(lineList)-1]
  else:
   recentRate = '<No data>'

  # Prepare html header and the rest of the page
  r = [HdrHtml]		# Output html header
  xt,rt,ft = GetTemperatures()
  s = '00'
  r.append(MainFormHtmlTemplate%(GetCurrentMode(comC),
                                 rt, GetRoomTargetT(), xt, s,
                                 GetCurrentHighFanMode(comC),
                                 GetCurrentBlindsMode(comR),
                                 recentRate))

  # Sync time with CondCtl
  t=time.localtime()
  WaitReply(comC, "AT*TMH=%d"%t.tm_hour)
  WaitReply(comC, "AT*TMM=%d"%t.tm_min)
  WaitReply(comC, "AT*TMS=%d"%t.tm_sec)
  WaitReply(comC, "AT*TMW=%d"%t.tm_wday)
  return r

########## Main web server entry

 path = environ.get('PATH_INFO', '').lstrip('/')
 r =["N/A"]

 if path == 'cgi-bin/genimg':
  start_response('200 OK', [('Content-type', 'image/png')])

  for i in range(3):
   try:
    r = GenImg()
   except PortError, x:
    Reconnect(x)
   else:
    break;
  return r

 elif path == 'cgi-bin/settings':
  start_response('200 OK', [('Content-Type','text/html; charset=ISO-8859-1')])
  r = SettingsPage()
  return r

 elif path == 'cgi-bin/condctl':
  start_response('200 OK', [('Content-Type','text/html; charset=ISO-8859-1')])
  for i in range(3):
   try:
    r = CondCtl()
   except PortError, x:
    Reconnect(x)
   else:
    break;
  return r

 else:
  start_response('404 NOT FOUND', [('Content-Type', 'text/html; charset=ISO-8859-1')])
  return ['Not Found']


def FindPort(name, rate):
 pdir = glob.glob(name)
 com = None
 s = "<None>"
 for s in pdir:
  try:
   com = OpenPort(s, rate);
  except serial.serialutil.SerialException:
   print "...failed opening port at \"%s\""%s;
   com = None
  else:
   break
 return (com, s)


# ===========================================
# Service thread class
#  -- gathering room temperature statistics, scheduler, other service actions
# ===========================================
class ServiceThreadClass(threading.Thread):
 def run(self):
  global RoomT, PrevExtT
  print("["+DateTime()+"] Service thread started")

  recentDay = datetime.datetime.now().day
  recentSampling = datetime.datetime.now()
  recentTime = recentSampling

  while 1:

    # Handle scheduler events
    newTime = datetime.datetime.now()
    today = datetime.date.today()
    with LockCfg:
      for evt in CfgEvents:
         if evt.evEnabled and ((1 << newTime.weekday()) & evt.evDays) != 0:
           eventTime = datetime.datetime.combine(today, evt.evTime)
           if eventTime > recentTime and eventTime <= newTime:
              print("["+DateTime()+"] Event triggered: "+evt.GenerateString())
#              evt.Execute()

    recentTime = newTime


    # Handle periodic sampling for graphs
    newSampling = datetime.datetime.now()
    if (newSampling - recentSampling).total_seconds() >= 12/3*60:	# 12/3 minutes passed, sample for graphs
      recentSampling = newSampling

      # Sample room & fridge temperature, store into the history array
      xt,rt,ft = GetTemperatures()
      tim = datetime.datetime.now()
      ndx = (tim.hour * 60 + tim.minute)/(12/3)
      RoomT[ndx] = rt
      FridgeT[ndx] = ft
      ExtT[ndx] = xt

      # Sample baro pressure
      p = GetBaroP()
      BaroP[ndx] = p

      print("["+DateTime()+"] Room T: %f, Baro P: %f"%(rt, p))

      # After midnight, once: Dump external temperature for the last 24hrs, shift last days graphs
      if recentDay != datetime.datetime.now().day:
        recentDay = datetime.datetime.now().day

        PrevExtT[1] = PrevExtT[0]
        PrevExtT[0] = ExtT
        logT = open('/www/cgi-bin/ext_temp.log',"a+")
        strTemp = map(lambda(x): str(x)+' ', Temp)
        logT.write("["+DateTime()+"] ")
        logT.writelines(strTemp[0:120])
        logT.write('\n')
        logT.close()

    time.sleep(60) # Sleep for 1 minute


# ===========================================
#  Main application, starting WSGI server
# ===========================================

print "\n*** CondSrv home CondCtl and other peripherals WSGI server ***"

# Create lock objects for accessing comC, comR and configuration state
LockC = threading.Lock();
LockR = threading.Lock();
LockCfg = threading.Lock();

# Create lists for various measurements
# 120*3 total, 15 per hour
ExtT = [0. for i in range(120*3+1)]
RoomT = [0. for i in range(120*3+1)]
FridgeT = [0. for i in range(120*3+1)]
BaroP = [0. for i in range(120*3+1)]

# Create list for saved previous days external measurements
# 120 total, 15 per hour
PrevExtT = [[0. for i in range(120*3+1)] for j in range(2)]

# Read global configuration variables from the database
print "["+DateTime()+"] Loading configuration"
sh = shelve.open('condsrv')
if 'CfgAcCtlEnabled' in sh:
  CfgAcCtlEnabled = sh['CfgAcCtlEnabled']
  print "["+DateTime()+"]  global state loaded"
else:
  print "["+DateTime()+"]  no global state loaded"
if 'CfgEvents' in sh:
  CfgEvents=sh['CfgEvents']
  print "["+DateTime()+"]  scheduler events loaded"
else:
  print "["+DateTime()+"]  no scheduler events loaded"
sh.close()

# Load previous day external temperatures from log
print "["+DateTime()+"] Loading previous day temperatures"
f = open('/www/cgi-bin/ext_temp.log',"r")
lineList = f.readlines()
f.close()
if len(lineList) >= 1:
  s = lineList[len(lineList)-1]
t = re.search('\[.*\] (.*) $', s)
if t:
  t = t.group(1).split(' ')
  PrevExtT[0] = map(lambda(x): int(x), t)

if len(lineList) >= 2:
  s = lineList[len(lineList)-2]
t = re.search('\[.*\] (.*) $', s)
if t:
  t = t.group(1).split(' ')
  PrevExtT[1] = map(lambda(x): int(x), t)

# Find and open peripheral com ports
(comC, nameC) = FindPort("/dev/ttyUSB*", 38400)
(comR, nameR) = FindPort("/dev/ttyACM*", 9600)

if comC:
 print "["+DateTime()+"] CondCtl port opened at \"%s\", 38400"%nameC
else:
 sys.exit("Cannot open CondCtl at ttyUSB*, exiting");

if comR:
 print "["+DateTime()+"] Arduino port opened at \"%s\", 9600"%nameR
else:
 sys.exit("Cannot open Arduino at ttyACM*, exiting");

time.sleep(2);					# Arduino de-glitching/startup

WaitReply(comC, "AT*ECB=200")

#pdb.set_trace()

#
# Start service thread for internal room statistics and aux control
#
ServiceThread = ServiceThreadClass()
ServiceThread.start()

#
# Create and start web server
#
srv = make_server('10.0.0.126', 80, application)
print("["+DateTime()+"] Starting web server...")

srv.serve_forever()

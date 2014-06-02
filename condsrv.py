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

sys.stderr = sys.stdout

import os, errno

import pdb

from cgi import parse_qs, escape
from wsgiref.simple_server import (make_server, WSGIRequestHandler)

LastRoomT = 0.	# Last measured room temperature
PrevExtT = [] 	# List of previous days external temperatures [2][120]
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
#       sys.exit("Timeout waiting for reply to command \""+cmd+"\"");
       raise PortError(com.port)

   if s and not re.search('^Temp\[*', s):
    sys.stdout.write("Re:\""+s+"\"\r\n");
 
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


def GetRoomT_FridgeT():
 global comC, LastRoomT
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
   m = re.search('30274600000056.*T=(\d*\.\d*)',l[i])
   if m:
     t = float(m.group(1))
     LastRoomT = t
     roomT = t

   m = re.search('AA8E5B02080074.*T=(\d*\.\d*)',l[i])
   if m:
     t = float(m.group(1))
     fridgeT = t

 print('Current RT:%f'%roomT)
 print('Current FT:%f'%fridgeT)
 return roomT, fridgeT


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
def GetExtTemp():
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
# CondCtl html page header
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

FormHtml = """
<img SRC="/cgi-bin/genimg">
<form>
<table cellpadding=5 cellspacing=5 border=1>
<tr><td>Mode: </td>    <td> %s</td><td><INPUT TYPE=submit NAME='CondCtlMode' VALUE='Off'><INPUT TYPE=submit NAME='CondCtlMode' VALUE='On'></td></tr>
<tr><td>T:%4.1f/%4.1f </td><td></td><td><INPUT TYPE=submit NAME='TargetT' VALUE='-'><INPUT TYPE=submit NAME='TargetT' VALUE='+'></td></tr>
<tr><td>High Fan: </td><td> %s</td><td><INPUT TYPE=submit NAME='HighFanMode' VALUE='Off'><INPUT TYPE=submit NAME='HighFanMode' VALUE='On'><INPUT TYPE=submit NAME='HighFanMode' VALUE='Forced'></td></tr>
<tr><td>Low Fan: </td> <td> %s</td><td><INPUT TYPE=submit NAME='LowFanMode'  VALUE='Off'><INPUT TYPE=submit NAME='LowFanMode'  VALUE='On'><INPUT TYPE=submit NAME='LowFanMode' VALUE='Forced'></td></tr>
<tr><td>Blinds: </td>  <td> %s</td><td><INPUT TYPE=submit NAME='BlindsMode'  VALUE='88'> <INPUT TYPE=submit NAME='BlindsMode' VALUE='90'><INPUT TYPE=submit NAME='BlindsMode' VALUE='95'><INPUT TYPE=submit NAME='BlindsMode' VALUE='100'></td></tr>
</table>
%s
"""


#
# Main WSGI application handler
#
def application(environ, start_response):
 global comC, comR, LockC, LockR, RoomT, FridgeT, BaroP

 def GenImg(comC, comR):

  (Temp,h,m,s) = GetExtTemp()

  # Generate plot
  stepX = 3
  sizeX = 120*stepX;
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

  curPos = (h*60*60+m*60+s)*sizeX / (24*60*60);
  draw.line([curPos, 1, curPos, sizeY-2], fill=strobeColor, width=1);

  tim = datetime.datetime.now()
  curNdx = (tim.hour * 60 + tim.minute)/12
  r = 3
  draw.ellipse([curPos-r, sizeY/2-Temp[curNdx]*3-r, curPos+r, sizeY/2-Temp[curNdx]*3+r], outline=lineColor)
  draw.ellipse([curPos-r, sizeY/2-((LastRoomT-20)*2)*3-r, curPos+r, sizeY/2-((LastRoomT-20)*2)*3+r], outline=lineIColor)
  
  nSamples = 120;

  for i in range(nSamples-1):
   draw.line([i*sizeX/nSamples, sizeY/2-PrevExtT[1][i]*3, 
             (i+1)*sizeX/nSamples, sizeY/2-PrevExtT[1][i+1]*3], fill=(190,190,255), width=1);

  for i in range(nSamples-1):
   draw.line([i*sizeX/nSamples, sizeY/2-PrevExtT[0][i]*3, 
             (i+1)*sizeX/nSamples, sizeY/2-PrevExtT[0][i+1]*3], fill=(130,130,255), width=1);

  for i in range(nSamples-1):
   draw.line([i*sizeX/nSamples, sizeY/2-Temp[i]*3, 
             (i+1)*sizeX/nSamples, sizeY/2-Temp[i+1]*3], fill=lineColor, width=1);

  nSamplesR = 120 * 3
  for i in range(nSamplesR):
   draw.point([i*sizeX/nSamplesR, sizeY/2-FridgeT[i]*3], fill=(255,200,255));
  
  for i in range(nSamplesR):
   draw.point([i*sizeX/nSamplesR, sizeY/2-((RoomT[i]-20)*2)*3], fill=lineIColor);

  for i in range(nSamplesR):
   draw.point([i*sizeX/nSamplesR, sizeY/2-(BaroP[i]-760-10)*3], fill=(224,86,27));

  
  draw.text([2,2], "%02d:%02d:%02d"%(h,m,s),  fill=strobeColor);

  f = cStringIO.StringIO()
  img.save(f, "PNG")

  f.seek(0)

  return [f.getvalue()]


 def CondCtl(comC, comR):

  def TargetTInc(com, cmd):
   t = GetRoomTargetT()
   t = t + 0.5
   WaitReply(com, 'AT*TGN=%d'%(t*10))
   

  def TargetTDec(com, cmd):
   t = GetRoomTargetT()
   t = t - 0.5
   WaitReply(com, 'AT*TGN=%d'%(t*10))
  
  commandMap = [
   ('CondCtlMode', 'On',    WaitReply, 'AT*MOD=3', comC),
   ('CondCtlMode', 'Off',   WaitReply, 'AT*MOD=0', comC),
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


  # Do peripherals configuration if requested
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
  r.append(FormHtml%(GetCurrentMode(comC), 
           	     GetRoomT_FridgeT()[0], GetRoomTargetT(),
                     GetCurrentHighFanMode(comC), 
                     GetCurrentLowFanMode(comC), 
                     GetCurrentBlindsMode(comR),
                     recentRate))
  
  # Sync time with CondCtl
  t=time.localtime()
  WaitReply(comC, "AT*TMH=%d"%t.tm_hour)
  WaitReply(comC, "AT*TMM=%d"%t.tm_min)
  WaitReply(comC, "AT*TMS=%d"%t.tm_sec)
  WaitReply(comC, "AT*TMW=%d"%t.tm_wday)
  return r

########## 

 path = environ.get('PATH_INFO', '').lstrip('/')
 r =["N/A"] 
 if path == 'cgi-bin/genimg':
  start_response('200 OK', [('Content-type', 'image/png')])

  for i in range(3):
   try:
    r = GenImg(comC, comR)
   except PortError, x:
    Reconnect(x)
   else:
    break;
 
  return r
 else:
  if path != 'cgi-bin/condctl':
   start_response('404 NOT FOUND', [('Content-Type', 'text/html; charset=ISO-8859-1')])
   return ['Not Found']
  else:
   start_response('200 OK', [('Content-Type','text/html; charset=ISO-8859-1')])

   for i in range(3):
    try:
     r = CondCtl(comC, comR)
    except PortError, x:
     Reconnect(x)
    else:
     break;

   return r
 
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


#
# Service thread class
#  -- gathering room temperature statistics, other service actions
#
class ServiceThreadClass(threading.Thread):
 def run(self): 
  global RoomT, PrevExtT
  print("["+DateTime()+"] Service thread started")

  recentDay = datetime.datetime.now().day
  
  while 1:
    # Sample room & fridge temperature, store into the history array
    rt,ft = GetRoomT_FridgeT()
    tim = datetime.datetime.now()
    ndx = (tim.hour * 60 + tim.minute)/(12/3)
    RoomT[ndx] = rt
    FridgeT[ndx] = ft

    # Sample baro pressure
    p = GetBaroP()
    BaroP[ndx] = p

    print("["+DateTime()+"] Room T: %f, Baro P: %f"%(rt, p))


    # Dump external temperature for the last 24hrs once after midnight, shift last days graphs
    if recentDay != datetime.datetime.now().day:
      recentDay = datetime.datetime.now().day
      (Temp,h,m,s) = GetExtTemp()
      PrevExtT[1] = PrevExtT[0]
      PrevExtT[0] = Temp
      logT = open('/www/cgi-bin/ext_temp.log',"a+")
      strTemp = map(lambda(x): str(x)+' ', Temp)
      logT.write("["+DateTime()+"] ")
      logT.writelines(strTemp[0:120])
      logT.write('\n')
      logT.close()
   
    time.sleep(12/3*60) # Sleep for 12/3 minutes, till the next measurement


# ===========================================
#  Main application, starting WSGI server
# ===========================================

print "\n*** CondSrv home CondCtl and other peripherals WSGI server ***"

# Create lock objects for accessing comC and comR
LockC = threading.Lock();
LockR = threading.Lock();

# Create lists for various measurements
# 120*3 total, 15 per hour
RoomT = [0. for i in range(120*3+1)]
FridgeT = [0. for i in range(120*3+1)]
BaroP = [0. for i in range(120*3+1)]

# Create list fpr saved previous days external measurements
# 120 total, 5 per hour
PrevExtT = [[0. for i in range(120+1)] for j in range(2)]

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
ServiceThread = ServiceThreadClass();
ServiceThread.start();

# 
# Create and start web server
#
srv = make_server('10.0.0.126', 80, application)
print("["+DateTime()+"] Starting web server...")

srv.serve_forever()

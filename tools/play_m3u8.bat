@echo off
rem Drag a local m3u8 created by StreamDownloader onto this file to play it with ffplay.
rem - Allows the local .key files (ffmpeg blocks non-media extensions by default), so encrypted segments play.
rem - Dropping video\fragments\media.m3u8 of a split download also plays the audio track:
rem   a master playlist backup\{title}\play.m3u8 is generated that links video and audio.
rem Keep this file ASCII only: cmd misreads UTF-8 batch files (paths with any language still work).
setlocal EnableExtensions

rem paths may contain ( ) or &, so they are never expanded inside ( ) blocks
set "ERR=Usage: drag an m3u8 file onto this script"
if "%~1"=="" goto fail
set "ERR=File not found:"
if not exist "%~f1" goto fail

rem ---------- find ffplay: PATH, then the folder of this script ----------
set "FFPLAY="
for %%P in (ffplay.exe) do set "FFPLAY=%%~$PATH:P"
if not defined FFPLAY if exist "%~dp0ffplay.exe" set "FFPLAY=%~dp0ffplay.exe"
set "ERR=ffplay.exe not found: add it to PATH or put it next to this script"
if not defined FFPLAY goto fail

rem newer ffmpeg also restricts segment extensions; relax it when supported
set "EXTRA="
"%FFPLAY%" -hide_banner -h demuxer=hls 2>nul | findstr /c:"allowed_segment_extensions" >nul && set "EXTRA=-allowed_segment_extensions ALL"

set "INPUT=%~f1"
set "TITLE=%~nx1"

rem ---------- split download: video\{fragments or decrypted}\x.m3u8 -> add the audio track ----------
for %%A in ("%~dp1.") do set "SUB=%%~nxA"
for %%A in ("%~dp1..") do set "TRACK=%%~nxA"
for %%A in ("%~dp1..\..") do set "ROOT=%%~fA"
if /i not "%TRACK%"=="video" goto play

set "AUDIO="
if exist "%ROOT%\audio\%SUB%\media.m3u8" set "AUDIO=audio/%SUB%/media.m3u8"
if not defined AUDIO if exist "%ROOT%\audio\fragments\media.m3u8" set "AUDIO=audio/fragments/media.m3u8"
if not defined AUDIO goto play

set "MASTER=%ROOT%\play.m3u8"
> "%MASTER%" echo #EXTM3U
>> "%MASTER%" echo #EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="audio",DEFAULT=YES,AUTOSELECT=YES,URI="%AUDIO%"
>> "%MASTER%" echo #EXT-X-STREAM-INF:BANDWIDTH=1,AUDIO="audio"
>> "%MASTER%" echo video/%SUB%/%~nx1
set "INPUT=%MASTER%"
for %%A in ("%ROOT%") do set "TITLE=%%~nxA"
echo Video: video/%SUB%/%~nx1
echo Audio: %AUDIO%

:play
echo Playing: "%INPUT%"
"%FFPLAY%" -hide_banner -loglevel error -allowed_extensions ALL %EXTRA% -window_title "%TITLE%" -i "%INPUT%"
if errorlevel 1 pause
exit /b

:fail
echo %ERR%
if not "%~1"=="" echo "%~f1"
pause
exit /b 1

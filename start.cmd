@echo off
rem ============================================================
rem  Double-click this file to open Frontier Radar in your browser.
rem  It starts a local web server on 127.0.0.1:8765 and opens it.
rem  Keep this window open while you use the app; close it to stop.
rem ============================================================
call "%~dp0run.cmd" serve --open %*

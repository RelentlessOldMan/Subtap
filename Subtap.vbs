' Subtap -- launch the desktop app with NO console window.
' Double-click this (instead of Subtap.cmd) to open just the app window.
' It runs pythonw.exe (console-less Python) hidden; the app opens in its own pywebview window.
' Requires pywebview:  pip install pywebview   (without it, use Subtap.cmd for browser mode).
Option Explicit
Dim shell, fso, here
Set shell = CreateObject("WScript.Shell")
Set fso   = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = here
' 0 = hidden window, False = don't wait. pythonw has no console, so nothing flashes.
shell.Run "pythonw.exe """ & here & "\subtap.py""", 0, False

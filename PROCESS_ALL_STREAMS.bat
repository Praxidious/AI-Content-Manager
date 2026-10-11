@echo off
rem Process this batch of finished, unprocessed streams and then open the trainer.
call "%~dp0process_stream.bat" --all-vods %*

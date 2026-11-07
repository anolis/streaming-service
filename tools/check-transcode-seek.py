import subprocess,tempfile
from pathlib import Path
from linuxcast import capture
with tempfile.TemporaryDirectory(prefix='linuxcast-seek-check-') as temp:
 root=Path(temp);source=root/'colors.mkv';out=root/'hls';out.mkdir()
 subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-f','lavfi','-i','color=red:s=32x32:r=5:d=2','-f','lavfi','-i','color=blue:s=32x32:r=5:d=2','-filter_complex','[0:v][1:v]concat=n=2:v=1:a=0[v]','-map','[v]','-c:v','ffv1',str(source)],check=True,timeout=20)
 command=capture.transcode_command(str(source),out,encoder='x264',start_seconds=2.5)
 subprocess.run(command,check=True,timeout=30)
 pixel=subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-i',str(out/capture.PLAYLIST),'-frames:v','1','-vf','crop=2:2:(iw-2)/2:(ih-2)/2,scale=1:1','-f','rawvideo','-pix_fmt','rgb24','pipe:1'],check=True,capture_output=True,timeout=20).stdout
 assert len(pixel)==3 and pixel[2]>200 and pixel[0]<30,pixel
 duration=capture.media_duration(capture.probe(str(out/capture.PLAYLIST)))
 assert 1 <= duration <= 2,duration
 print('PASS: seeking past red footage produces blue first frame and only the remaining duration', list(pixel), duration)

const fs = require('fs'), path = require('path'), { spawnSync } = require('child_process');
const D = __dirname, run = args => { const r = spawnSync('ffmpeg', args, { encoding: 'utf8', maxBuffer: 1 << 24 }); if (r.status) { console.error(r.stderr); process.exit(r.status); } };
const file = (name, text) => { const p = path.join(D, name); fs.mkdirSync(path.dirname(p), { recursive: true }); fs.writeFileSync(p, text); return p; };
const font = 'arial.ttf';
file('title.txt', 'BORROWED STEPS\nHuman approved community lending');
file('close.txt', 'BORROWED STEPS\nLive release: borrowed-steps.vercel.app\nCode: github.com/AtchayamG/borrowed-steps');
fs.mkdirSync(path.join(D, 'captions'), { recursive: true });
file('captions/c1.txt', 'LIVE HOSTED RELEASE\nVercel Hobby  •  Neon Free  •  ₹0 spend');
file('captions/c2.txt', 'HUMAN IN THE LOOP\nEvery reservation requires explicit volunteer confirmation');
file('captions/c3.txt', 'STRANDS + GROQ ADVISORY\nRead-only draft; deterministic workflow stays available');

const title = path.join(D, 'title.mp4'), close = path.join(D, 'close.mp4');
run(['-y','-loglevel','error','-f','lavfi','-i','color=c=0x0b1220:s=1920x1080:r=30','-vf',`drawtext=fontfile=${font}:textfile=title.txt:fontcolor=white:fontsize=64:line_spacing=18:x=(w-text_w)/2:y=(h-text_h)/2`, '-t','4','-c:v','libx264','-pix_fmt','yuv420p',title]);
run(['-y','-loglevel','error','-f','lavfi','-i','color=c=0x102a24:s=1920x1080:r=30','-vf',`drawtext=fontfile=${font}:textfile=close.txt:fontcolor=white:fontsize=52:line_spacing=18:x=(w-text_w)/2:y=(h-text_h)/2`, '-t','16','-c:v','libx264','-pix_fmt','yuv420p',close]);
run(['-y','-loglevel','error','-f','concat','-safe','0','-i',path.join(D,'concat.txt'),'-vf','scale=1920:1080,format=yuv420p','-r','30','-c:v','libx264','-preset','medium','-crf','23','-an',path.join(D,'main.mp4')]);
file('parts.txt', `file '${title.replace(/\\/g,'/')}'\nfile '${path.join(D,'main.mp4').replace(/\\/g,'/')}'\nfile '${close.replace(/\\/g,'/')}'\n`);
run(['-y','-loglevel','error','-f','concat','-safe','0','-i',path.join(D,'parts.txt'),'-c','copy',path.join(D,'Borrowed-Steps-demo.mp4')]);
// Add concise timed lower thirds to the live segment (which starts at 4s).
const input = path.join(D,'Borrowed-Steps-demo.mp4'), captioned = path.join(D,'Borrowed-Steps-demo-captioned.mp4');
const vf = [
  `drawbox=x=60:y=h-210:w=760:h=120:color=0x0b1220@0.86:t=fill:enable='between(t,4,15)'`,
  `drawtext=fontfile=${font}:textfile=captions/c1.txt:fontcolor=white:fontsize=30:line_spacing=8:x=90:y=h-185:enable='between(t,4,15)'`,
  `drawbox=x=60:y=h-210:w=940:h=120:color=0x0b1220@0.86:t=fill:enable='between(t,30,42)'`,
  `drawtext=fontfile=${font}:textfile=captions/c2.txt:fontcolor=white:fontsize=30:line_spacing=8:x=90:y=h-185:enable='between(t,30,42)'`,
  `drawbox=x=60:y=h-210:w=900:h=120:color=0x0b1220@0.86:t=fill:enable='between(t,42,50)'`,
  `drawtext=fontfile=${font}:textfile=captions/c3.txt:fontcolor=white:fontsize=30:line_spacing=8:x=90:y=h-185:enable='between(t,42,50)'`
].join(',');
run(['-y','-loglevel','error','-i',input,'-vf',vf,'-c:v','libx264','-preset','medium','-crf','23','-pix_fmt','yuv420p','-an',path.join(D,'Borrowed-Steps-demo-captioned.mp4')]);
fs.renameSync(path.join(D,'Borrowed-Steps-demo-captioned.mp4'), path.join(D,'Borrowed-Steps-demo-video.mp4'));
console.log('VIDEO', path.join(D,'Borrowed-Steps-demo-video.mp4'));

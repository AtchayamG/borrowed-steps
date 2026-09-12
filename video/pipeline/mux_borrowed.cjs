const { spawnSync } = require('child_process');
const fs = require('fs'), path = require('path');
const D = __dirname, segs = JSON.parse(fs.readFileSync(path.join(D,'vo-layout.json'),'utf8'));
const args = ['-y','-loglevel','error','-i',path.join(D,'Borrowed-Steps-demo-video.mp4')];
for (const s of segs) args.push('-i',path.join(D,'vo',s.id+'.mp3'));
let fc = '';
segs.forEach((s,i) => { fc += `[${i+1}:a]aformat=sample_rates=48000:channel_layouts=stereo,adelay=${Math.round(s.start*1000)}:all=1[a${i}];`; });
fc += segs.map((_,i)=>`[a${i}]`).join('') + `amix=inputs=${segs.length}:normalize=0:dropout_transition=0[mix];[mix]loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000,apad[aout]`;
const out = path.join(D,'Borrowed-Steps-demo-final.mp4');
const r = spawnSync('ffmpeg',[...args,'-filter_complex',fc,'-map','0:v','-map','[aout]','-c:v','copy','-c:a','aac','-b:a','160k','-ar','48000','-ac','2','-t','68','-movflags','+faststart',out],{encoding:'utf8',maxBuffer:1<<24});
if (r.status) { console.error(r.stderr); process.exit(r.status); }
console.log('FINAL',out,fs.statSync(out).size);

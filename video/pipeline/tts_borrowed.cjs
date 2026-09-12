const { spawnSync } = require('child_process');
const fs = require('fs'), path = require('path');
const D = __dirname, VO = path.join(D, 'vo2'), EDGE = 'C:/Users/Atchayam/AppData/Local/Programs/Python/Python312/Scripts/edge-tts.exe';
const segs = JSON.parse(fs.readFileSync(path.join(D, 'narration.json'), 'utf8'));
fs.rmSync(VO, { recursive: true, force: true }); fs.mkdirSync(VO, { recursive: true });
const dur = f => parseFloat((spawnSync('ffprobe',['-v','error','-show_entries','format=duration','-of','csv=p=0',f],{encoding:'utf8'}).stdout||'0').trim()) || 0;
let previous = 0;
for (const s of segs) {
  const out = path.join(VO, s.id + '.mp3');
  const r = spawnSync(EDGE, ['--voice','en-US-AndrewNeural','--rate=-4%','--text',s.text,'--write-media',out], { encoding:'utf8', maxBuffer:1<<20 });
  if (r.status || !fs.existsSync(out)) { console.error(r.stderr || r.stdout); process.exit(1); }
  s.start = Math.max(s.at, previous + 0.25); s.duration = +dur(out).toFixed(2); s.end = +(s.start + s.duration).toFixed(2); previous = s.end;
}
fs.writeFileSync(path.join(D,'vo-layout.json'), JSON.stringify(segs,null,2));
console.log(segs.map(s => `${s.id} ${s.start}s ${s.duration}s`).join('\n'));

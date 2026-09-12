const fs = require('fs'), path = require('path');
const dir = __dirname;
const manifest = JSON.parse(fs.readFileSync(path.join(dir, 'manifest.json'), 'utf8'));
const frames = manifest.frames;
let concat = '', total = 0;
const mapped = [];
for (let i = 0; i < frames.length; i++) {
  const gap = (i + 1 < frames.length ? frames[i + 1].t - frames[i].t : 500) / 1000;
  const duration = Math.max(0.033, Math.min(3.5, gap));
  mapped.push({ name: manifest.marks.find(m => m.t <= frames[i].t)?.name, srcMs: frames[i].t, outSec: total });
  concat += `file 'frames/${frames[i].f}'\nduration ${duration.toFixed(3)}\n`;
  total += duration;
}
if (frames.length) concat += `file 'frames/${frames[frames.length - 1].f}'\n`;
fs.writeFileSync(path.join(dir, 'concat.txt'), concat);
const toOut = ms => { let best = 0; for (const f of frames) { if (f.t <= ms) best = mapped[frames.indexOf(f)].outSec; else break; } return +best.toFixed(2); };
const marks = manifest.marks.map(m => ({ name: m.name, src: m.t, out: toOut(m.t) }));
fs.writeFileSync(path.join(dir, 'marks-mapped.json'), JSON.stringify({ outDuration: +total.toFixed(2), marks }, null, 2));
console.log('outDuration', total.toFixed(2));
console.log(marks.map(m => `${m.name}=${m.out}`).join(' '));

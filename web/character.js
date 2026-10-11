/** Original abstract audio indicator for the public release; no third-party character assets. */
export class XiaoyunCharacter {
  constructor(svg) {
    this.svg = svg;
    svg.setAttribute('viewBox', '0 0 240 240');
    svg.innerHTML = '<circle cx="120" cy="120" r="84" fill="#D97527"/>' +
      Array.from({length: 5}, (_, i) => `<rect x="${74+i*20}" y="103" width="12" height="34" rx="6" fill="#fff8ed"/>`).join('');
    this.bars = [...svg.querySelectorAll('rect')];
    this.level = 0;
  }
  update(state, level=0) {
    if (document.hidden) return;
    const target = state==='speaking' ? Math.min(1,Math.max(0,Number(level)||0)) : state==='listening' ? .25 : state==='thinking' ? .12 : 0;
    this.level += (target-this.level)*.16;
    const t=performance.now()/1000;
    this.bars.forEach((bar,i) => {
      const height=24+this.level*66*(.65+.35*Math.sin(t*4+i*.8));
      bar.setAttribute('y',String(120-height/2));bar.setAttribute('height',String(height));
    });
    this.svg.setAttribute('aria-label',`小萌音频状态：${state}`);
  }
  destroy() { this.svg.replaceChildren();this.bars=[]; }
}

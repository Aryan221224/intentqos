// IntentQoS hardware test setup diagram -> SVG -> PNG
const React = require('react'), S = require('react-dom/server'), sharp = require('sharp');
const fa = require('react-icons/fa'), md = require('react-icons/md'), bs = require('react-icons/bs');
const out = process.argv[2];
const W = 1800, H = 1130;
const C = { navy: '#0B2A4A', ink: '#1E293B', mute: '#4A5566', blue: '#0A8FD0', orange: '#E07B39', orangeD: '#C55A11',
  green: '#2E8B57', greenT: '#E6F4EC', blueT: '#E6F3FB', peach: '#FFF1E6', grey: '#9AA5B1', card: '#F3F7FB', edge: '#D5DFEA',
  gold: '#F2A93B', lav: '#8E7CC3', lavT: '#ECE7F7', lavD: '#5B45A3' };
const F = 'Carlito, Calibri, Arial, sans-serif';
let svg = [];
const esc = t => t.replace(/&/g, '&amp;').replace(/</g, '&lt;');
function icon(Comp, x, y, s, color) {
  const m = S.renderToStaticMarkup(React.createElement(Comp, { size: s, color }));
  svg.push(m.replace(/ (width|height)="[^"]*"/g, "").replace("<svg ", `<svg x="${x}" y="${y}" width="${s}" height="${s}" `));
}
function text(x, y, t, o = {}) {
  const lines = Array.isArray(t) ? t : [t];
  const sz = o.size || 18, lh = o.lh || sz * 1.22;
  lines.forEach((ln, i) => {
    const parts = Array.isArray(ln) ? ln : [[ln, {}]];
    svg.push(`<text x="${x}" y="${y + i * lh}" font-family="${F}" font-size="${sz}" fill="${o.color || C.ink}" text-anchor="${o.anchor || 'start'}" font-weight="${o.bold ? 700 : 400}" font-style="${o.italic ? 'italic' : 'normal'}">` +
      parts.map(([s, p]) => `<tspan font-weight="${p.bold ?? o.bold ? 700 : 400}" fill="${p.color || o.color || C.ink}">${esc(s)}</tspan>`).join('') + '</text>');
  });
}
function rect(x, y, w, h, o = {}) {
  svg.push(`<rect x="${x}" y="${y}" width="${w}" height="${h}" rx="${o.r ?? 14}" fill="${o.fill || '#fff'}" stroke="${o.stroke || C.edge}" stroke-width="${o.sw ?? 2}" ${o.dash ? `stroke-dasharray="${o.dash}"` : ''}/>`);
}
function line(pts, o = {}) {
  const d = 'M' + pts.map(p => p.join(',')).join(' L');
  svg.push(`<path d="${d}" fill="none" stroke="${o.color || C.ink}" stroke-width="${o.w || 4}" ${o.dash ? `stroke-dasharray="${o.dash}"` : ''} stroke-linecap="round" stroke-linejoin="round" ${o.arrow ? `marker-end="url(#ar-${o.arrow})"` : ''}/>`);
}
function buy(x, y, t = 'BUY') {
  rect(x, y, 54, 24, { fill: C.gold, stroke: '#fff', sw: 2, r: 12 });
  text(x + 27, y + 17, t, { size: 13, bold: true, color: C.navy, anchor: 'middle' });
}
function device(x, y, w, Comp, title, sub, o = {}) {
  const h = o.h || 86;
  rect(x, y, w, h, { fill: o.fill || '#fff', stroke: o.stroke || C.edge, sw: o.sw || 2 });
  const cx = x + 16, cy = y + (h - 52) / 2;
  svg.push(`<circle cx="${cx + 26}" cy="${cy + 26}" r="28" fill="${o.circle || C.navy}"/>`);
  icon(Comp, cx + 8, cy + 8, 36, '#fff');
  if (o.prog) {
    text(x + 84, y + h / 2 - 16, title, { size: 19, bold: true });
    text(x + 84, y + h / 2 + 6, o.prog, { size: 15, bold: true, color: C.lavD });
    if (sub) text(x + 84, y + h / 2 + 27, sub, { size: 14, color: C.mute });
    return;
  }
  text(x + 84, y + h / 2 - (sub ? 6 : -6), title, { size: 19, bold: true });
  if (sub) text(x + 84, y + h / 2 + 16, sub, { size: 15, color: C.mute });
}
const defs = `<defs>${Object.entries({ ink: C.ink, blue: C.blue, orange: C.orangeD, green: C.green, lav: C.lav, grey: C.grey })
  .map(([k, c]) => `<marker id="ar-${k}" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="${c}"/></marker>`).join('')}</defs>`;

// background + title
svg.push(`<rect width="${W}" height="${H}" fill="#ffffff"/>`);
text(60, 62, 'IntentQoS 2.0: hardware setup and the software on each device', { size: 38, bold: true, color: C.navy });
text(60, 96, 'Real home line, a GL.iNet OpenWrt router running IntentQoS 2.0.0, a phone as the 4G backup, and the household devices that make the traffic. Purple names are kit programs.', { size: 19, color: C.mute });

// ---------------- zones
rect(40, 130, 470, 910, { fill: C.card, stroke: C.edge, r: 20 });
text(60, 165, 'INTERNET SIDE', { size: 17, bold: true, color: C.mute });
rect(560, 130, 520, 910, { fill: '#FAFCFE', stroke: C.edge, r: 20 });
text(580, 165, 'THE GATEWAY', { size: 17, bold: true, color: C.mute });
rect(1130, 130, 630, 910, { fill: C.card, stroke: C.edge, r: 20 });
text(1150, 165, 'HOME SIDE (TEST TRAFFIC)', { size: 17, bold: true, color: C.mute });

// ---------------- internet side
// cloud
svg.push(`<circle cx="275" cy="235" r="48" fill="${C.blue}"/>`);
icon(fa.FaCloud, 245, 205, 60, '#fff');
text(275, 310, 'Internet', { size: 20, bold: true, anchor: 'middle' });
// ISP router
device(80, 360, 390, md.MdRouter, 'ISP router / fibre ONT', '100 Mbps fibre (primary); nothing installed', { circle: C.blue });
line([[275, 318], [275, 358]], { color: C.blue, w: 5 });
text(290, 344, 'fibre', { size: 15, color: C.blue, italic: true });
// 4G path
svg.push(`<circle cx="160" cy="560" r="34" fill="${C.orangeD}"/>`);
icon(fa.FaBroadcastTower, 140, 540, 40, '#fff');
text(210, 552, '4G network', { size: 18, bold: true });
text(210, 574, 'mobile operator', { size: 15, color: C.mute });
device(80, 630, 390, fa.FaMobileAlt, 'Team phone (4G)', 'USB tethering on; nothing installed', { circle: C.orangeD, stroke: C.orange, fill: C.peach });
line([[160, 596], [160, 628]], { color: C.orangeD, w: 4, dash: '4 7' });
device(80, 800, 390, fa.FaLaptop, 'Bridge laptop', 'shares the phone’s 4G over Ethernet', { h: 100, prog: 'IntentQoS 4G Bridge.exe', circle: C.orangeD, stroke: C.orange, fill: C.peach });
line([[275, 718], [275, 798]], { color: C.orangeD, w: 4, dash: '3 7' });
text(290, 765, 'USB tethering', { size: 15, color: C.orangeD, italic: true });
buy(400, 790, 'opt.');
text(80, 945, ['Optional: TP-Link UE300 USB-to-Ethernet adapter', 'if the bridge laptop has no Ethernet port'], { size: 14, color: C.mute });

// ---------------- gateway
const gx = 590, gy = 330, gw = 460, gh = 330;
rect(gx, gy, gw, gh, { fill: C.navy, stroke: C.gold, sw: 4, r: 22 });
svg.push(`<circle cx="${gx + 60}" cy="${gy + 62}" r="38" fill="#fff"/>`);
icon(md.MdRouter, gx + 34, gy + 36, 52, C.navy);
text(gx + 115, gy + 55, 'GL.iNet GL-MT3000', { size: 26, bold: true, color: '#fff' });
text(gx + 115, gy + 82, 'OpenWrt router (Beryl AX), ARM64', { size: 17, color: '#C9D8E8' });
buy(gx + gw - 74, gy + 18);
rect(gx + 20, gy + 104, gw - 40, 132, { fill: '#123B63', stroke: '#2C5A86', r: 14 });
text(gx + 38, gy + 132, 'IntentQoS 2.0.0 engine runs here', { size: 20, bold: true, color: C.gold });
text(gx + 38, gy + 156, ['classifies flows, measures the line, shapes with tc', 'scores each app (QoO), tells Wi-Fi from line limits',
  'outcome control, boost offers, CAMARA, signed updates', 'capture helper (aarch64), watchdog, agent, console :8080'], { size: 15, lh: 19, color: '#E6EEF6' });
// ports
rect(gx - 18, gy + 246, 150, 62, { fill: '#fff', stroke: C.blue, sw: 3, r: 12 });
text(gx + 57, gy + 272, 'WAN port', { size: 18, bold: true, anchor: 'middle', color: C.blue });
text(gx + 57, gy + 294, 'upload shaped here', { size: 14, anchor: 'middle', color: C.mute });
rect(gx + gw - 132, gy + 246, 150, 62, { fill: '#fff', stroke: C.green, sw: 3, r: 12 });
text(gx + gw - 57, gy + 272, 'LAN + Wi-Fi', { size: 18, bold: true, anchor: 'middle', color: C.green });
text(gx + gw - 57, gy + 294, 'download shaped here', { size: 14, anchor: 'middle', color: C.mute });

// Router Setup: any laptop, once
device(590, 190, 460, fa.FaLaptop, 'Any laptop on the router (once)', 'installs the engine over SSH, pairs it with the console', { h: 100, prog: 'IntentQoS Router Setup.exe', circle: C.lav, stroke: C.lav, fill: C.lavT });
line([[820, 292], [820, gy - 4]], { color: C.lav, w: 3, dash: '2 7', arrow: 'lav' });
text(832, 316, 'SSH install', { size: 14, color: C.lav, italic: true });

// WAN cables
line([[470, 403], [535, 403], [535, gy + 277], [gx - 22, gy + 277]], { color: C.blue, w: 6, arrow: 'blue' });
text(488, 392, 'Ethernet', { size: 15, color: C.blue, italic: true });
line([[470, 850], [535, 850], [535, gy + 292], [gx - 22, gy + 292]], { color: C.orangeD, w: 5, dash: '14 9', arrow: 'orange' });
// failover callout
rect(560, 700, 500, 104, { fill: C.peach, stroke: C.orangeD, sw: 2.5, r: 14 });
text(580, 732, 'Failover test', { size: 20, bold: true, color: C.orangeD });
text(580, 758, ['move the router’s WAN cable from the ISP router', 'to the bridge laptop: same port, slower 4G path'], { size: 16 });

// operator console
device(590, 845, 460, fa.FaDesktop, 'Operator laptop', 'controller, CAMARA API, private stats, phone app, test endpoints', { h: 110, prog: 'IntentQoS Operator Console.exe', circle: C.lav, stroke: C.lav, fill: C.lavT });
text(590, 985, 'Can be the same laptop as the test laptop. Ports 8443, 8088, 8446/8447, UDP 47815.', { size: 14, color: C.mute });
line([[820, 841], [820, 808]], { color: C.lav, w: 3, dash: '2 7' });
text(832, 830, 'management (router agent dials out)', { size: 14, color: C.lav, italic: true });

// ---------------- home side
// switch
const sx = 1170, sy = 220;
device(sx, sy, 300, bs.BsHddNetwork, '5-port gigabit switch', 'TP-Link TL-SG1005D', { circle: C.green });
buy(sx + 236, sy - 12, 'opt.');
line([[gx + gw + 18, gy + 270], [1110, gy + 270], [1110, sy + 43], [sx - 4, sy + 43]], { color: C.green, w: 6, arrow: 'green' });
text(1122, 480, 'Ethernet', { size: 15, color: C.green, italic: true });
// wired devices
const WD = [[fa.FaLaptop, 'Work call', 'Meet / Teams', null], [fa.FaDesktop, 'Download + backup', 'ISO + cloud backup', null],
  [fa.FaLaptop, 'Test laptop', 'off vs on · QoO · RPM · capacity', 'IntentQoS Test Kit.exe']];
WD.forEach(([I, t, s, p], k) => {
  const y = 330 + k * 104;
  device(1400, y, 345, I, t, s, { h: p ? 96 : 80, prog: p, circle: p ? C.lav : C.navy, stroke: p ? C.lav : C.edge, fill: p ? C.lavT : '#fff' });
  line([[sx + 150, sy + 88], [sx + 150, y + 40], [1396, y + 40]], { color: C.green, w: 3.5, arrow: 'green' });
});
text(1180, 410, 'wired', { size: 16, color: C.green, italic: true, bold: true });
// wifi devices
svg.push(`<circle cx="1210" cy="${700}" r="34" fill="${C.green}"/>`);
icon(fa.FaWifi, 1190, 680, 40, '#fff');
text(1255, 694, 'Wi-Fi from the router', { size: 18, bold: true });
text(1255, 716, 'household devices; nothing installed except the app', { size: 15, color: C.mute });
line([[gx + gw + 18, gy + 292], [1100, gy + 292], [1100, 700], [1172, 700]], { color: C.green, w: 4, dash: '2 9', arrow: 'green' });
const WF = [[fa.FaTv, '3 TVs streaming', 'TV, stick, or laptops on YouTube', null], [fa.FaGamepad, 'Game', 'PC, console or phone', null],
  [fa.FaMobileAlt, 'Phone', 'scan the console’s QR, Add to Home screen; offers, boost in plain words', 'SampleNet Home app (web app from the console)']];
WF.forEach(([I, t, s, p], k) => {
  device(1170, 760 + k * 74, 570, I, t, s, { h: p ? 96 : 64, prog: p, circle: p ? C.lav : C.blue, stroke: p ? C.lav : C.edge, fill: p ? C.lavT : '#fff' });
});

// ---------------- legend
const lx = 60, ly = 1085;
line([[lx, ly], [lx + 50, ly]], { color: C.blue, w: 6 }); text(lx + 60, ly + 6, 'Ethernet, primary line', { size: 15 });
line([[lx + 250, ly], [lx + 300, ly]], { color: C.orangeD, w: 5, dash: '14 9' }); text(lx + 310, ly + 6, '4G backup path', { size: 15 });
line([[lx + 460, ly], [lx + 510, ly]], { color: C.green, w: 5 }); text(lx + 520, ly + 6, 'home LAN', { size: 15 });
line([[lx + 630, ly], [lx + 680, ly]], { color: C.green, w: 4, dash: '2 9' }); text(lx + 690, ly + 6, 'Wi-Fi', { size: 15 });
buy(lx + 780, ly - 13); text(lx + 842, ly + 6, 'to buy', { size: 15 });
buy(lx + 920, ly - 13, 'opt.'); text(lx + 982, ly + 6, 'optional buy', { size: 15 });
rect(lx + 1100, ly - 13, 26, 26, { fill: C.lavT, stroke: C.lav, r: 6 }); text(lx + 1136, ly + 6, 'runs an IntentQoS kit program (version 2.0.0)', { size: 15 });

const doc = `<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}">${defs}${svg.join('')}</svg>`;
require('fs').writeFileSync(out.replace(/\.png$/, '.svg'), doc);
sharp(Buffer.from(doc), { density: 144 }).png().toFile(out).then(() => console.log('ok'));

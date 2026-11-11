/**
 * Radar — Hermes Desktop panel ("Mesa"). Unified package: this file + dashboard/plugin_api.py.
 *
 * Every task is drawn as the object of its world — a trip is a boarding pass, a purchase a receipt, a
 * work item a PR ticket, a commitment a calendar leaf, a check a caution label, a routine a dial, anything
 * else a filed note — rendered as a disciplined paper object on a calm, light worktable. Card SIZE follows
 * urgency and importance; each card reads its urgency as a small signal meter. Kinds the model designs for
 * repeated "other" tasks arrive in `card_kinds` and reuse these layouts with their own hue and icon.
 */
import {
  ROUTES_AREA, SIDEBAR_NAV_AREA, SandboxedFrame, Badge, Button, ScrollArea, Skeleton,
  useQuery, queryClient, relativeTime, cn,
} from '@hermes/plugin-sdk'
import { jsx, jsxs, Fragment } from 'react/jsx-runtime'
import { useState, useEffect, useMemo, useRef } from 'react'

const BOARD_KEY = ['radar', 'board']
let rest = null

const TEXT = {
  es: {
    title: 'Radar', tasks: 'en seguimiento', working: 'trabajando', youHave: 'te tocan', refresh: 'Actualizar',
    board: 'Tablero', rules: 'Reglas', empty: 'Nada en el radar.', emptyHint: 'Cuando llegue algo que merezca tu atención, aparecerá aquí.',
    error: 'El panel no se pudo cargar: ', proposals: 'Propuestas', proposal: 'propuesta', checklist: 'Pendiente',
    draft: 'Propongo responder', context: 'Correo', later: 'Recordar mañana', mute: 'No avisar más', done: 'Ya está resuelto',
    report: 'Ver informe', hideReport: 'Ocultar informe', waiting: 'en espera', executing: 'en marcha',
    nothingPending: 'Nada que decidir ahora.', due: 'Vence', jobRunning: 'trabajando', priorityHigh: 'prioridad alta',
    forget: 'Quitar', dismiss: 'Descartar', confirm: 'Confirmar', save: 'Guardar', instruction: 'Qué quieres que cambie…', sender: 'remitente (opcional)',
    effects: { silent: 'no avisar', digest: 'solo resumen', notify: 'avisar siempre', prefer: 'preferir' },
    status: { needs_user: 'Te toca', in_progress: 'En marcha', open: 'Abiertas', waiting: 'Esperando', snoozed: 'Pospuestas' },
    mutedGroup: 'Silenciadas', unmute: 'Reactivar', muted: 'Silenciada: no volverá a avisar.', unmuted: 'Reactivada.',
    patterns: 'Patrones', preferences: 'Reglas de atención', suggestions: 'Te propone una regla', kinds: 'Tipos de tarjeta',
    selected: 'seleccionadas', doneN: 'marcadas como hechas', muteN: 'silenciadas', laterN: 'pospuestas', clearSel: 'Quitar selección',
    send: 'Enviar', sent: 'Anotado, lo retomo', composePlaceholder: 'Escribe para continuar la tarea…', composeHint: 'Se lo paso al asistente para que siga · ⌘+Enter',
    stackMore: (n) => `${n} parecidas · ver todas`, stackCollapse: (n) => `Apilar ${n}`,
    noPatterns: 'Aún no hay patrones.', noPrefs: 'Sin reglas guardadas.', daysLeft: (n) => (n === 0 ? 'hoy' : n === 1 ? 'mañana' : n < 0 ? `hace ${-n} días` : `faltan ${n} días`),
    depart: 'Salida', back: 'Vuelta', total: 'Total', order: 'Pedido', vendor: 'Vendedor', delivery: 'Entrega', branch: 'rama', pr: 'PR en borrador',
    draftReady: 'borrador listo', every: 'Horario', question: 'Comprobar', kindsHint: 'Los tipos nuevos salen de tareas repetidas.',
    effect: 'Efecto', scope: 'Ámbito', origin: 'Origen', details: 'Detalle', close: 'Cerrar', stateLabel: 'Estado',
    originMap: { explicit: 'tú la creaste', inferred: 'aprendida', imported: 'importada' },
  },
  en: {
    title: 'Radar', tasks: 'tracked', working: 'working', youHave: 'need you', refresh: 'Refresh',
    board: 'Board', rules: 'Rules', empty: 'Nothing on the radar.', emptyHint: 'When something deserves your attention, it shows up here.',
    error: 'The panel could not load: ', proposals: 'Proposals', proposal: 'proposal', checklist: 'Pending',
    draft: 'Suggested reply', context: 'Email', later: 'Remind me tomorrow', mute: 'Stop notifying', done: 'Already resolved',
    report: 'Show report', hideReport: 'Hide report', waiting: 'waiting', executing: 'running',
    nothingPending: 'Nothing to decide right now.', due: 'Due', jobRunning: 'working', priorityHigh: 'high priority',
    forget: 'Remove', dismiss: 'Dismiss', confirm: 'Confirm', save: 'Save', instruction: 'What should change…', sender: 'sender (optional)',
    effects: { silent: 'never notify', digest: 'digest only', notify: 'always notify', prefer: 'prefer' },
    status: { needs_user: 'Your call', in_progress: 'In progress', open: 'Open', waiting: 'Waiting', snoozed: 'Snoozed' },
    mutedGroup: 'Muted', unmute: 'Unmute', muted: 'Muted: it will not notify again.', unmuted: 'Unmuted.',
    patterns: 'Patterns', preferences: 'Attention rules', suggestions: 'Suggested rule', kinds: 'Card kinds',
    selected: 'selected', doneN: 'marked done', muteN: 'muted', laterN: 'snoozed', clearSel: 'Clear selection',
    send: 'Send', sent: 'Noted, I\'ll pick it up', composePlaceholder: 'Write to continue this task…', composeHint: 'Goes to the assistant to continue · ⌘+Enter',
    stackMore: (n) => `${n} similar · show all`, stackCollapse: (n) => `Stack ${n}`,
    noPatterns: 'No patterns yet.', noPrefs: 'No rules saved.', daysLeft: (n) => (n === 0 ? 'today' : n === 1 ? 'tomorrow' : n < 0 ? `${-n} days ago` : `${n} days left`),
    depart: 'Departs', back: 'Returns', total: 'Total', order: 'Order', vendor: 'Vendor', delivery: 'Delivery', branch: 'branch', pr: 'draft PR',
    draftReady: 'draft ready', every: 'Schedule', question: 'Check', kindsHint: 'New kinds come from repeated tasks.',
    effect: 'Effect', scope: 'Scope', origin: 'Origin', details: 'Detail', close: 'Close', stateLabel: 'Status',
    originMap: { explicit: 'you created it', inferred: 'learned', imported: 'imported' },
  },
}
const STATUS_ORDER = ['needs_user', 'in_progress', 'open', 'waiting', 'snoozed']
/* material hue per layout — fixed, reads on the light worktable */
const TINT = { 'boarding-pass': 'var(--rd-navy)', receipt: 'var(--rd-olive)', ticket: 'var(--rd-slate)', calendar: 'var(--rd-sky)', tag: 'var(--rd-brick)', dial: 'var(--rd-sky)', note: 'var(--rd-dim)' }

const STYLE = `
.rd-root{
  --rd-desk:#eeeeec; --rd-desk2:#f7f7f5; --rd-paper:#ffffff; --rd-panel:#f4f4f2;
  --rd-ink:#1e1d1b; --rd-sub:#44423c; --rd-dim:#78746b; --rd-faint:#a3a096;
  --rd-line:#e4e3de; --rd-hair:#eeedea; --rd-accent:#176961;
  --rd-navy:#1f3a5c; --rd-slate:#4a4664; --rd-brick:#aa3b2d; --rd-amber:#b9821c; --rd-olive:#7e7033; --rd-sky:#2f6d8c;
  --rd-sh:0 1px 2px rgba(48,40,24,.07), 0 12px 26px -12px rgba(48,40,24,.30);
  --rd-shb:0 1px 2px rgba(48,40,24,.09), 0 20px 40px -16px rgba(48,40,24,.34);
  --rd-ui:"Archivo","Inter var",system-ui,sans-serif; --rd-mono:"JetBrains Mono",ui-monospace,SFMono-Regular,monospace;
  position:relative; height:100%; display:flex; flex-direction:column; color:var(--rd-ink); background:
    linear-gradient(0deg,rgba(0,0,0,.015),rgba(0,0,0,.015)) 0 0/100% 29px,
    radial-gradient(140% 70% at 50% -16%,var(--rd-desk2),transparent 60%), var(--rd-desk);
  background-blend-mode:multiply,normal,normal;
}
.rd-root *{box-sizing:border-box}
.rd-head{display:flex;flex-wrap:wrap;align-items:center;gap:12px 20px;justify-content:space-between;padding:13px 16px;background:var(--rd-panel);border-bottom:1px solid var(--rd-line)}
.rd-brand{display:flex;align-items:center;gap:11px;min-width:0}
.rd-scope{width:26px;height:26px;flex:none;color:var(--rd-accent)}
.rd-nm{font-family:var(--rd-ui);font-weight:800;letter-spacing:.14em;text-transform:uppercase;font-size:.95rem;line-height:1}
.rd-st{font-family:var(--rd-mono);font-size:.74rem;color:var(--rd-dim);margin-top:3px}
.rd-st b{color:var(--rd-ink);font-weight:600}
.rd-actions{display:flex;gap:7px}
.rd-ghost{font-family:var(--rd-ui);font-weight:600;font-size:.74rem;letter-spacing:.03em;color:var(--rd-sub);border:1px solid var(--rd-line);background:var(--rd-paper);padding:7px 12px;border-radius:9px;cursor:pointer}
.rd-ghost:hover{border-color:var(--rd-dim)}
.rd-ghost.on{background:var(--rd-ink);color:var(--rd-paper);border-color:var(--rd-ink)}
.rd-ghost:focus-visible,.rd-obj:focus-visible{outline:2px solid var(--rd-accent);outline-offset:2px}

.rd-board{display:grid;grid-template-columns:1fr 288px;gap:18px;padding:18px 16px 24px;align-items:start}
.rd-bento{display:grid;grid-template-columns:repeat(6,1fr);grid-auto-rows:auto;gap:15px;grid-auto-flow:row;min-width:0;align-content:start;align-items:start}
.rd-secrow{grid-column:1/-1;display:flex;align-items:center;gap:11px;margin-top:2px}
.rd-secrow .l{font-family:var(--rd-ui);font-weight:700;font-size:.66rem;letter-spacing:.2em;text-transform:uppercase;color:var(--rd-dim)}
.rd-secrow .ln{flex:1;height:1px;background:var(--rd-line)}
.rd-secrow .c{font-family:var(--rd-mono);font-size:.7rem;color:var(--rd-faint)}

.rd-bento{position:relative;user-select:none}
.rd-xl{grid-column:span 4}.rd-lg{grid-column:span 3}.rd-md{grid-column:span 2}
@media (max-width:820px){.rd-board{grid-template-columns:1fr}}
@media (max-width:620px){.rd-bento{grid-template-columns:repeat(2,1fr)}.rd-xl,.rd-lg{grid-column:span 2}.rd-md{grid-column:span 1}}
@media (max-width:430px){.rd-bento{grid-template-columns:1fr}.rd-xl,.rd-lg,.rd-md{grid-column:span 1}}
.rd-obj[data-checked="true"]{box-shadow:inset 0 0 0 2px var(--rd-accent),var(--rd-sh);background:color-mix(in srgb,var(--rd-accent) 6%,var(--rd-paper))}
.rd-obj.rd-leaving{animation:rd-leave .24s ease forwards;pointer-events:none}
@keyframes rd-leave{to{opacity:0;transform:translateX(26px) scale(.97)}}
.rd-marquee{position:absolute;z-index:4;border:1px solid var(--rd-accent);background:color-mix(in srgb,var(--rd-accent) 13%,transparent);border-radius:4px;pointer-events:none}
.rd-batchbar{position:absolute;left:50%;bottom:16px;transform:translateX(-50%);z-index:6;display:flex;align-items:center;gap:10px;background:var(--rd-ink);color:var(--rd-paper);border-radius:13px;padding:8px 10px 8px 15px;box-shadow:var(--rd-shb);font-size:.85rem;max-width:94%}
.rd-batchbar b{font-family:var(--rd-ui);white-space:nowrap}
.rd-batchbar button{font-family:var(--rd-ui);font-weight:600;font-size:.78rem;border-radius:9px;padding:7px 12px;border:0;background:rgba(255,255,255,.14);color:var(--rd-paper);cursor:pointer;white-space:nowrap}
.rd-batchbar button:hover{background:rgba(255,255,255,.24)}
.rd-batchbar .x{background:transparent;opacity:.7;padding:7px 8px}

.rd-obj{position:relative;min-width:0;background:var(--rd-paper);border:1px solid var(--rd-line);border-radius:13px;box-shadow:var(--rd-sh);transition:transform .15s ease,box-shadow .15s ease;overflow:hidden;cursor:pointer;text-align:left;width:100%;color:inherit;font:inherit;display:block}
.rd-obj:hover{transform:translateY(-2px);box-shadow:var(--rd-shb)}
.rd-obj[data-sel="true"]{box-shadow:0 0 0 2px var(--rd-ink) inset,var(--rd-sh)}
.rd-pad{padding:13px 14px;display:flex;flex-direction:column;gap:8px}
.rd-tl{display:flex;align-items:center;gap:8px;min-width:0}
.rd-glyph{width:27px;height:27px;border-radius:8px;flex:none;display:grid;place-items:center;font-size:13px;background:color-mix(in srgb,var(--tc,#777) 13%,var(--rd-paper));color:var(--tc,var(--rd-ink));border:1px solid color-mix(in srgb,var(--tc,#777) 22%,var(--rd-line))}
.rd-ko{font-family:var(--rd-ui);font-weight:700;font-size:.62rem;letter-spacing:.15em;text-transform:uppercase;color:var(--tc,var(--rd-dim));white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
.rd-ttl{font-weight:650;font-size:1rem;line-height:1.26;letter-spacing:-.01em}
.rd-lg .rd-ttl{font-size:1.14rem;font-weight:700}
.rd-sum{color:var(--rd-sub);font-size:.85rem;line-height:1.45;min-width:0}
.rd-facts{display:flex;flex-wrap:wrap;gap:6px}
.rd-f{font-family:var(--rd-mono);font-size:.71rem;color:var(--rd-ink);background:var(--rd-panel);border:1px solid var(--rd-line);border-radius:6px;padding:2px 7px;white-space:nowrap}
.rd-f s{color:var(--rd-dim);text-decoration:none}
.rd-sig{margin-left:auto;display:flex;align-items:flex-end;gap:2.5px;height:13px;flex:none}
.rd-sig i{width:3px;border-radius:1px;background:var(--rd-line)}
.rd-sig i:nth-child(1){height:5px}.rd-sig i:nth-child(2){height:8px}.rd-sig i:nth-child(3){height:10px}.rd-sig i:nth-child(4){height:13px}
.rd-sig i.on{background:var(--tc,var(--rd-dim))}

/* caution / tag */
.rd-tag{--tc:var(--rd-brick)}
.rd-haz{height:5px;background:repeating-linear-gradient(45deg,var(--rd-amber) 0 10px,#2a251b 10px 20px);opacity:.9}
.rd-band{display:flex;align-items:center;gap:8px;padding:8px 14px;background:color-mix(in srgb,var(--rd-brick) 7%,var(--rd-paper));border-bottom:1px solid var(--rd-line)}
.rd-sev{font-family:var(--rd-ui);font-weight:800;letter-spacing:.11em;text-transform:uppercase;font-size:.64rem;color:var(--rd-brick)}

/* boarding pass */
.rd-pass{--tc:var(--rd-navy);display:grid;grid-template-columns:1fr 92px}
.rd-pm{padding:13px 15px;display:flex;flex-direction:column;gap:9px;border-right:1px dashed #dbdad5;position:relative}
.rd-pm::before,.rd-pm::after{content:"";position:absolute;right:-6px;width:11px;height:11px;border-radius:50%;background:var(--rd-desk);border:1px solid var(--rd-line)}
.rd-pm::before{top:-6px}.rd-pm::after{bottom:-6px}
.rd-air{font-family:var(--rd-ui);font-weight:700;letter-spacing:.18em;text-transform:uppercase;font-size:.56rem;color:var(--rd-navy)}
.rd-route{font-family:var(--rd-ui);font-weight:900;font-size:1.7rem;line-height:1;letter-spacing:-.01em;color:var(--rd-navy);display:flex;flex-wrap:wrap;align-items:baseline;gap:9px}
.rd-route .ar{font-size:.9rem;color:var(--rd-dim)}
.rd-pg{display:flex;gap:16px;flex-wrap:wrap}
.rd-pg .k{display:block;font-family:var(--rd-ui);font-size:.54rem;letter-spacing:.09em;text-transform:uppercase;color:var(--rd-dim)}
.rd-pg b{font-family:var(--rd-mono);font-weight:600;font-size:.8rem}
.rd-stub{background:var(--rd-navy);color:#eaf0f7;padding:12px 9px;display:flex;flex-direction:column;justify-content:space-between;text-align:center;gap:8px}
.rd-stub .k{font-size:.48rem;letter-spacing:.13em;text-transform:uppercase;opacity:.72}
.rd-stub .v{font-family:var(--rd-ui);font-weight:900;font-size:.98rem}
.rd-bar{height:22px;background:repeating-linear-gradient(90deg,currentColor 0 2px,transparent 2px 3px,currentColor 3px 4px,transparent 4px 7px);opacity:.8;border-radius:1px}

/* ticket */
.rd-ticket{--tc:var(--rd-slate);display:flex}
.rd-spine{background:var(--rd-slate);color:#eeecf5;writing-mode:vertical-rl;text-orientation:mixed;font-family:var(--rd-ui);font-weight:800;letter-spacing:.18em;font-size:.56rem;padding:8px 5px;display:flex;align-items:center;justify-content:center}
.rd-prn{font-family:var(--rd-mono);font-weight:600;color:var(--rd-slate);font-size:.78rem;overflow-wrap:anywhere}

/* receipt */
.rd-rp{padding:12px 14px 15px;display:flex;flex-direction:column;gap:4px;position:relative;font-family:var(--rd-mono)}
.rd-rp::after{content:"";position:absolute;left:0;right:0;bottom:0;height:5px;background:repeating-linear-gradient(135deg,var(--rd-paper) 0 6px,transparent 6px 12px);filter:drop-shadow(0 1px 0 var(--rd-line))}
.rd-store{font-family:var(--rd-ui);font-weight:700;letter-spacing:.13em;font-size:.7rem;text-align:center;padding-bottom:5px;border-bottom:1px dashed #dbdad5}
.rd-li{display:flex;justify-content:space-between;gap:10px;font-size:.72rem;color:var(--rd-sub)}
.rd-li.t{border-top:1px dashed #dbdad5;padding-top:4px;margin-top:2px;color:var(--rd-ink);font-weight:700}
.rd-rst{text-align:center;font-size:.6rem;color:var(--rd-dim);margin-top:3px}

/* calendar leaf */
.rd-cal{--tc:var(--rd-sky);display:flex}
.rd-leaf{flex:none;min-width:62px;padding:11px 9px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:1px;border-right:1px solid var(--rd-line);background:color-mix(in srgb,var(--rd-sky) 8%,var(--rd-paper));position:relative}
.rd-leaf .mo{font-family:var(--rd-ui);font-weight:700;font-size:.56rem;letter-spacing:.1em;text-transform:uppercase;color:var(--rd-sky)}
.rd-leaf .dy{font-family:var(--rd-ui);font-weight:900;font-size:1.5rem;line-height:1}
.rd-leaf .wd{font-size:.58rem;color:var(--rd-dim)}

/* dial */
.rd-gauge{--p:60;width:58px;height:58px;flex:none;border-radius:50%;display:grid;place-items:center;position:relative;background:conic-gradient(var(--rd-sky) calc(var(--p)*1%),#e7e6e2 0)}
.rd-gauge::after{content:"";position:absolute;inset:8px;border-radius:50%;background:var(--rd-paper)}
.rd-gauge b{position:relative;font-family:var(--rd-ui);font-weight:900;font-size:.98rem}

/* note */
.rd-nstripe{position:absolute;left:0;top:0;bottom:0;width:4px;background:var(--rd-brick);opacity:.5}
.rd-npad{padding-left:16px}
.rd-hl{font-family:var(--rd-ui);font-weight:800;font-size:1.1rem;letter-spacing:-.01em}

.rd-chip{display:inline-flex;align-items:center;gap:4px;white-space:nowrap;padding:1px 7px;border-radius:999px;border:1px solid var(--rd-line);font-size:.66rem;color:var(--rd-dim);font-family:var(--rd-ui);font-weight:600}
.rd-chip[data-live="true"]::before{content:"";width:6px;height:6px;border-radius:50%;background:#3f8f52;animation:rd-pulse 1.6s ease-in-out infinite}

/* stacked similar tasks */
.rd-stackcard{position:relative;background:var(--rd-paper);border:1px solid var(--rd-line);border-radius:13px;box-shadow:var(--rd-sh);cursor:pointer;padding:13px 15px;display:flex;flex-direction:column;gap:7px;width:100%;text-align:left;color:inherit;font:inherit;transition:transform .15s ease}
.rd-stackcard::before,.rd-stackcard::after{content:"";position:absolute;left:0;right:0;top:0;bottom:0;border:1px solid var(--rd-line);border-radius:13px;background:var(--rd-paper);z-index:-1}
.rd-stackcard::before{transform:translate(4px,5px)}
.rd-stackcard::after{transform:translate(8px,10px);opacity:.55}
.rd-stackcard:hover{transform:translateY(-2px)}
.rd-stackn{margin-left:auto;font-family:var(--rd-ui);font-weight:800;font-size:.78rem;background:var(--tc,var(--rd-slate));color:#fff;border-radius:999px;padding:1px 9px;flex:none}
.rd-stackmore{font-family:var(--rd-ui);font-weight:600;font-size:.72rem;color:var(--rd-accent)}
.rd-collapse{border:1px dashed var(--rd-line);border-radius:13px;background:transparent;color:var(--rd-dim);font-family:var(--rd-ui);font-weight:600;font-size:.8rem;cursor:pointer;min-height:72px;display:flex;align-items:center;justify-content:center;gap:6px}
.rd-collapse:hover{border-color:var(--rd-dim);color:var(--rd-sub)}

/* rail */
.rd-rail{display:flex;flex-direction:column;gap:15px;min-width:0}
.rd-card{background:var(--rd-panel);border:1px solid var(--rd-line);border-radius:13px;padding:13px 14px}
.rd-card h4{font-family:var(--rd-ui);font-weight:700;font-size:.66rem;letter-spacing:.15em;text-transform:uppercase;color:var(--rd-dim);display:flex;justify-content:space-between;align-items:center;margin:0 0 10px}
.rd-rule{display:flex;align-items:center;gap:9px;font-size:.83rem;padding:6px 0;border-top:1px solid var(--rd-hair)}
.rd-rule:first-of-type{border-top:0}
.rd-rule .d{width:7px;height:7px;border-radius:2px;flex:none}
.rd-rule .x{margin-left:auto;color:var(--rd-dim);font-size:.72rem;text-decoration:underline;text-underline-offset:2px;cursor:pointer}
.rd-rulebtn{display:flex;align-items:center;gap:9px;width:100%;background:transparent;border:0;border-top:1px solid var(--rd-hair);cursor:pointer;color:inherit;font:inherit;font-size:.83rem;padding:8px 6px;text-align:left;border-radius:7px}
.rd-rulebtn:first-of-type{border-top:0}
.rd-rulebtn:hover,.rd-rulebtn:focus-visible{background:var(--rd-panel);outline:none}
.rd-rulebtn .d{width:7px;height:7px;border-radius:2px;flex:none}
.rd-rulebtn .nm{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
.rd-rulebtn .ef{font-size:.66rem;color:var(--rd-dim);font-family:var(--rd-ui);font-weight:600;flex:none;white-space:nowrap}
.rd-rulebtn .ca{color:var(--rd-faint);flex:none;font-size:.82rem}
.rd-sug{border:1px solid color-mix(in srgb,var(--rd-amber) 45%,var(--rd-line));background:color-mix(in srgb,var(--rd-amber) 8%,var(--rd-paper));border-radius:12px;padding:12px 13px}
.rd-sug .k{font-family:var(--rd-ui);font-weight:800;font-size:.6rem;letter-spacing:.13em;text-transform:uppercase;color:var(--rd-amber)}
.rd-sug p{margin:7px 0 10px;font-size:.84rem;line-height:1.44}
.rd-input{font:inherit;font-size:.82rem;border:1px solid var(--rd-line);background:var(--rd-paper);border-radius:8px;padding:6px 9px;color:var(--rd-ink);min-width:0}
.rd-empty{padding:52px 16px;text-align:center}
.rd-empty .t{font-weight:650;font-size:1rem}
.rd-empty .h{font-size:.84rem;color:var(--rd-dim);margin-top:4px}
.rd-banner{margin:10px 16px -4px;border:1px solid var(--rd-line);border-left:3px solid var(--rd-accent);border-radius:9px;background:var(--rd-paper);padding:9px 12px;font-size:.82rem}
.rd-banner.err{border-left-color:var(--rd-brick);color:var(--rd-brick)}

/* focus modal — the card lifts and centres, the rest dims, a panel appears beside it */
.rd-scrim{position:absolute;inset:0;z-index:8;background:rgba(26,22,16,.46);display:flex;align-items:center;justify-content:center;padding:24px;animation:rd-fade .16s ease}
.rd-focus{display:flex;gap:18px;width:100%;max-width:920px;max-height:88%;animation:rd-pop .2s cubic-bezier(.2,.8,.3,1)}
.rd-focus-card{flex:0 0 330px;max-width:330px;align-self:center;min-width:0}
.rd-focus-card .rd-obj{cursor:default;box-shadow:0 34px 64px -26px rgba(26,22,16,.66),var(--rd-sh)}
.rd-focus-panel{flex:1 1 auto;min-width:0;background:var(--rd-paper);border:1px solid var(--rd-line);border-radius:16px;box-shadow:var(--rd-shb);display:flex;flex-direction:column;max-height:88vh;overflow:hidden}
.rd-focus-head{display:flex;align-items:flex-start;justify-content:space-between;gap:10px;padding:15px 16px 11px;border-bottom:1px solid var(--rd-line)}
.rd-focus-eyebrow{font-family:var(--rd-ui);font-weight:700;font-size:.63rem;letter-spacing:.14em;text-transform:uppercase;color:var(--rd-dim)}
.rd-focus-title{font-size:1.1rem;font-weight:700;line-height:1.22;margin-top:3px}
.rd-focus-body{padding:14px 16px;overflow:auto;display:flex;flex-direction:column;gap:12px;min-height:0;flex:1}
.rd-compose{border-top:1px solid var(--rd-line);padding:11px 14px;display:flex;flex-direction:column;gap:8px;background:var(--rd-panel)}
.rd-compose textarea{font:inherit;font-size:.86rem;border:1px solid var(--rd-line);border-radius:10px;padding:9px 11px;resize:vertical;min-height:52px;background:var(--rd-paper);color:var(--rd-ink)}
.rd-compose .row{display:flex;justify-content:space-between;align-items:center;gap:8px}
.rd-compose .hint{font-size:.72rem;color:var(--rd-dim);min-width:0}
/* info modal — a rule or a pattern opened for its full detail */
.rd-info{width:100%;max-width:430px;max-height:84%;background:var(--rd-paper);border:1px solid var(--rd-line);border-radius:16px;box-shadow:var(--rd-shb);display:flex;flex-direction:column;overflow:hidden;animation:rd-pop .2s cubic-bezier(.2,.8,.3,1)}
.rd-info-head{display:flex;align-items:flex-start;justify-content:space-between;gap:10px;padding:15px 16px 12px;border-bottom:1px solid var(--rd-line)}
.rd-info-eyebrow{display:flex;align-items:center;gap:7px;font-family:var(--rd-ui);font-weight:700;font-size:.63rem;letter-spacing:.14em;text-transform:uppercase;color:var(--rd-dim)}
.rd-info-title{font-size:1.06rem;font-weight:700;line-height:1.25;margin-top:4px;word-break:break-word}
.rd-info-body{padding:6px 16px 14px;overflow:auto;min-height:0}
.rd-info-row{display:grid;grid-template-columns:84px 1fr;gap:10px;padding:9px 0;border-top:1px solid var(--rd-hair);font-size:.85rem;line-height:1.45}
.rd-info-row:first-child{border-top:0}
.rd-info-row .k{font-family:var(--rd-ui);font-weight:600;font-size:.64rem;letter-spacing:.1em;text-transform:uppercase;color:var(--rd-dim);padding-top:2px}
.rd-info-row .v{min-width:0;color:var(--rd-sub);word-break:break-word}
.rd-info-foot{display:flex;flex-wrap:wrap;gap:7px;justify-content:flex-end;padding:11px 14px;border-top:1px solid var(--rd-line);background:var(--rd-panel)}
@keyframes rd-fade{from{opacity:0}to{opacity:1}}
@keyframes rd-pop{from{opacity:0;transform:scale(.97) translateY(6px)}to{opacity:1;transform:none}}
@keyframes rd-pulse{0%,100%{opacity:.4}50%{opacity:1}}
@media (max-width:760px){.rd-focus{flex-direction:column;overflow:auto}.rd-focus-card{flex:none;max-width:100%;align-self:stretch}.rd-focus-panel{max-height:none}}
@media (prefers-reduced-motion:reduce){.rd-obj,.rd-scrim,.rd-focus{transition:none;animation:none}.rd-chip[data-live="true"]::before{animation:none}}
`

function ago(value) {
  const ms = value ? Date.parse(value) : NaN
  if (!Number.isFinite(ms) || typeof relativeTime !== 'function') return ''
  try { return relativeTime(ms) } catch (_err) { return '' }
}
function useBoard() { return useQuery({ queryKey: BOARD_KEY, queryFn: () => rest('/board'), refetchInterval: 30000, staleTime: 5000 }) }
function invalidate() { queryClient.invalidateQueries({ queryKey: BOARD_KEY }) }
async function post(path, body) { const result = await rest(path, { method: 'POST', body }); invalidate(); return result }

function kindOf(board, task) {
  const kinds = (board && board.card_kinds) || {}
  return kinds[task.card_kind] || kinds[task.kind] || kinds.other || { label: {}, icon: '•', hue: 0, layout: 'note' }
}
function label(kind, language) { return (kind.label && (kind.label[language] || kind.label.es || kind.label.en)) || '' }
function layoutOf(task, kind) { return (task.artifact && task.artifact.layout) || kind.layout || 'note' }
function tintOf(task, kind) { return TINT[layoutOf(task, kind)] || 'var(--rd-dim)' }

/* priority: urgency of open decisions + whether it is the owner's call + due proximity + work in flight */
function scoreOf(task) {
  const ps = task.proposals || []
  const urg = ps.reduce((m, p) => Math.max(m, p.urgency === 'high' ? 3 : p.urgency === 'normal' ? 2 : p.urgency ? 1 : 0), 0)
  const decision = ps.some((p) => p.action_class === 'external' || p.operation === 'ask' || p.operation === 'check')
  const due = task.due_at ? Date.parse(task.due_at) : NaN
  const soon = Number.isFinite(due) && due - Date.now() < 3 * 864e5
  let s = urg
  if (task.status === 'needs_user') s += 2
  if (decision) s += 1
  if (soon) s += 1
  if (task.draft) s += 1
  if (task.jobs_running) s += 1
  return s
}
function sizeClass(s) { return s >= 4 ? 'rd-lg' : 'rd-md' }
function bars(s) { return Math.max(1, Math.min(4, Math.round(s / 2))) }
function Sig({ n }) { return jsx('span', { className: 'rd-sig', children: [0, 1, 2, 3].map((i) => jsx('i', { className: i < n ? 'on' : '' }, i)) }) }

function chips(task, t) {
  const pending = (task.proposals || []).filter((p) => p.status === 'pending' || p.status === 'sent').length
  const out = []
  if (task.jobs_running) out.push(jsx('span', { className: 'rd-chip', 'data-live': 'true', children: t.jobRunning }, 'j'))
  if (task.draft) out.push(jsx('span', { className: 'rd-chip', children: t.draftReady }, 'd'))
  if (pending) out.push(jsx('span', { className: 'rd-chip', children: `${pending} ${pending === 1 ? t.proposal : t.proposals.toLowerCase()}` }, 'p'))
  return out
}

function tagline(task, kind, t, language, score, extra) {
  return jsxs('div', { className: 'rd-tl', children: [
    jsx('span', { className: 'rd-glyph', children: kind.icon }),
    jsx('span', { className: 'rd-ko', children: extra || label(kind, language) }),
    jsx(Sig, { n: bars(score) }),
  ] })
}

function BoardingPass({ task, kind, t, language, score }) {
  const a = task.artifact || {}
  const to = a.to && a.to !== a.from ? a.to : ''
  return jsxs('div', { className: 'rd-pass', children: [
    jsxs('div', { className: 'rd-pm', children: [
      jsx('div', { className: 'rd-air', children: `${t.title} · ${label(kind, language)}` }),
      jsxs('div', { className: 'rd-route', children: [jsx('span', { children: a.from || '—' }), to ? jsx('span', { className: 'ar', children: '→' }) : null, to ? jsx('span', { children: to }) : null] }),
      jsx('div', { className: 'rd-ttl', style: { fontSize: '.92rem' }, children: task.title }),
      jsxs('div', { className: 'rd-pg', children: [
        a.depart && a.depart.day ? jsxs('div', { children: [jsx('span', { className: 'k', children: t.depart }), jsx('b', { children: `${a.depart.day} ${a.depart.month}${a.depart.time && a.depart.time !== '00:00' ? ' · ' + a.depart.time : ''}` })] }) : null,
        a.return && a.return.day ? jsxs('div', { children: [jsx('span', { className: 'k', children: t.back }), jsx('b', { children: `${a.return.day} ${a.return.month}` })] }) : null,
      ] }),
      jsx('div', { className: 'rd-bar', style: { color: '#1b2430' } }),
    ] }),
    jsxs('div', { className: 'rd-stub', children: [
      jsxs('div', { children: [jsx('div', { className: 'k', children: 'PNR' }), jsx('div', { className: 'v', children: a.code ? String(a.code).slice(0, 6) : '—' })] }),
      jsx('div', { className: 'rd-bar', style: { color: '#cdd8e6', height: '34px' } }),
      jsx(Sig, { n: bars(score) }),
    ] }),
  ] })
}

function Receipt({ task, kind, t, language, score }) {
  const a = task.artifact || {}
  const amount = a.amount && a.amount.value != null ? `${a.amount.value} ${a.amount.currency || ''}`.trim() : ''
  return jsxs('div', { className: 'rd-rp', children: [
    jsx('div', { className: 'rd-store', children: a.vendor || label(kind, language) }),
    ...(a.lines || []).slice(0, 2).map((line, i) => jsxs('div', { className: 'rd-li', children: [jsx('span', { style: { overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }, children: line }), jsx('span', { children: '·' })] }, i)),
    a.lines && a.lines.length ? null : jsxs('div', { className: 'rd-li', children: [jsx('span', { style: { overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }, children: task.title }), jsx('span', { children: '·' })] }),
    amount ? jsxs('div', { className: 'rd-li t', children: [jsx('span', { children: t.total.toUpperCase() }), jsx('span', { children: amount })] }) : jsxs('div', { className: 'rd-li t', children: [jsx('span', { children: task.title }), jsx('span', { children: '✓' })] }),
    jsx('div', { className: 'rd-rst', children: task.summary ? String(task.summary).slice(0, 48) : '' }),
  ] })
}

function Ticket({ task, kind, t, language, score }) {
  const a = task.artifact || {}
  return jsxs('div', { className: 'rd-ticket', children: [
    jsx('div', { className: 'rd-spine', children: a.repo ? String(a.repo).split('/').pop() : label(kind, language) }),
    jsxs('div', { className: 'rd-pad', style: { flex: 1 }, children: [
      jsxs('div', { className: 'rd-tl', children: [jsx('span', { className: 'rd-ko', children: a.repo || label(kind, language) }), jsx(Sig, { n: bars(score) })] }),
      a.number || a.branch ? jsx('div', { className: 'rd-prn', children: [a.number, a.branch ? `⎇ ${a.branch}` : '', a.pr ? t.pr : ''].filter(Boolean).join(' · ') }) : null,
      jsx('div', { className: 'rd-ttl', children: task.title }),
      task.summary ? jsx('div', { className: 'rd-sum', style: { display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical', overflow: 'hidden' }, children: task.summary }) : null,
      chips(task, t).length ? jsx('div', { className: 'rd-facts', children: chips(task, t) }) : null,
    ] }),
  ] })
}

function Tag({ task, kind, t, language, score }) {
  const a = task.artifact || {}
  const sev = a.severity || a.level || ''
  return jsxs(Fragment, { children: [
    jsx('div', { className: 'rd-haz' }),
    jsxs('div', { className: 'rd-band', children: [jsx('span', { children: kind.icon }), jsx('span', { className: 'rd-sev', children: sev ? String(sev) : t.question }), jsx(Sig, { n: bars(score) })] }),
    jsxs('div', { className: 'rd-pad', children: [
      jsx('div', { className: 'rd-ttl', children: task.title }),
      a.question || task.summary ? jsx('div', { className: 'rd-sum', children: a.question || task.summary }) : null,
    ] }),
  ] })
}

function CalendarLeaf({ task, kind, t, language, score }) {
  const a = task.artifact || {}; const due = a.due || {}
  return jsxs('div', { className: 'rd-cal', children: [
    jsxs('div', { className: 'rd-leaf', children: [jsx('span', { className: 'mo', children: due.month || '' }), jsx('span', { className: 'dy', children: due.day != null ? String(due.day) : kind.icon }), jsx('span', { className: 'wd', children: due.weekday || '' })] }),
    jsxs('div', { className: 'rd-pad', style: { flex: 1 }, children: [
      jsxs('div', { className: 'rd-tl', children: [jsx('span', { className: 'rd-ko', children: a.days_left != null ? t.daysLeft(a.days_left) : label(kind, language) }), jsx(Sig, { n: bars(score) })] }),
      jsx('div', { className: 'rd-ttl', children: task.title }),
      task.summary ? jsx('div', { className: 'rd-sum', children: task.summary }) : null,
    ] }),
  ] })
}

function Dial({ task, kind, t, language, score }) {
  const a = task.artifact || {}
  const pct = a.percent != null ? a.percent : 60
  const val = a.value != null ? a.value : (kind.icon || '•')
  return jsxs('div', { className: 'rd-pad', children: [
    jsxs('div', { className: 'rd-tl', children: [jsx('span', { className: 'rd-ko', children: a.schedule_label || label(kind, language) }), jsx(Sig, { n: bars(score) })] }),
    jsxs('div', { style: { display: 'flex', alignItems: 'center', gap: '13px', minWidth: 0 }, children: [
      jsx('div', { className: 'rd-gauge', style: { '--p': String(pct) }, children: jsx('b', { children: String(val) }) }),
      jsxs('div', { style: { minWidth: 0 }, children: [jsx('div', { className: 'rd-ttl', style: { fontSize: '.92rem' }, children: task.title }), task.summary ? jsx('div', { className: 'rd-sum', style: { fontSize: '.78rem' }, children: task.summary }) : null] }),
    ] }),
  ] })
}

function Note({ task, kind, t, language, score }) {
  const a = task.artifact || {}
  return jsxs(Fragment, { children: [
    jsx('div', { className: 'rd-nstripe' }),
    jsxs('div', { className: 'rd-pad rd-npad', children: [
      jsxs('div', { className: 'rd-tl', children: [jsx('span', { className: 'rd-ko', children: a.eyebrow || label(kind, language) }), jsx(Sig, { n: bars(score) })] }),
      a.headline ? jsx('div', { className: 'rd-hl', children: a.headline }) : null,
      jsx('div', { className: 'rd-ttl', children: task.title }),
      task.summary ? jsx('div', { className: 'rd-sum', children: task.summary }) : null,
    ] }),
  ] })
}

const LAYOUTS = { 'boarding-pass': BoardingPass, receipt: Receipt, ticket: Ticket, calendar: CalendarLeaf, tag: Tag, dial: Dial, note: Note }

function Card({ task, kind, t, language, selected, checked, leaving, onSelect, onToggle }) {
  const layout = layoutOf(task, kind)
  const Inner = LAYOUTS[layout] || Note
  const score = scoreOf(task)
  const size = layout === 'boarding-pass' ? 'rd-xl' : sizeClass(score)  // a boarding pass reads across, not tall: give it width
  const onClick = (e) => { if (e.metaKey || e.ctrlKey || e.shiftKey) { e.preventDefault(); onToggle(task.id) } else onSelect(task.id) }
  return jsx('button', { type: 'button', className: cn('rd-obj', size, leaving && 'rd-leaving'), 'data-id': task.id,
    style: { '--tc': tintOf(task, kind) }, 'data-sel': selected ? 'true' : 'false', 'data-checked': checked ? 'true' : 'false', onClick,
    children: jsx(Inner, { task, kind, t, language, score }) })
}

function stackKey(task) { return (task.entities && task.entities.repo) || task.card_kind || task.kind || '' }
function makeUnits(items) {  // group similar tasks; a group of 3+ becomes one collapsible stack
  const by = {}
  items.forEach((it) => { const k = stackKey(it); (by[k] = by[k] || []).push(it) })
  const units = []
  Object.keys(by).forEach((k) => {
    const arr = by[k].slice().sort((a, b) => scoreOf(b) - scoreOf(a))
    if (k && arr.length >= 3) units.push({ stack: true, key: k, items: arr, score: scoreOf(arr[0]) })
    else arr.forEach((it) => units.push({ stack: false, item: it, score: scoreOf(it) }))
  })
  return units.sort((a, b) => b.score - a.score)
}
function StackCard({ unit, data, t, language, onExpand }) {
  const items = unit.items
  const top = items[0]
  const kind = kindOf(data, top)
  const name = (top.entities && top.entities.repo) || label(kind, language)
  return jsxs('button', { type: 'button', className: cn('rd-stackcard', 'rd-md'), style: { '--tc': tintOf(top, kind) }, onClick: onExpand, children: [
    jsxs('div', { className: 'rd-tl', children: [jsx('span', { className: 'rd-glyph', children: kind.icon }), jsx('span', { className: 'rd-ko', children: name }), jsx('span', { className: 'rd-stackn', children: String(items.length) })] }),
    jsx('div', { className: 'rd-ttl', style: { fontSize: '.95rem' }, children: top.title }),
    items[1] ? jsx('div', { className: 'rd-sum', style: { fontSize: '.8rem', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }, children: items[1].title }) : null,
    jsx('div', { className: 'rd-stackmore', children: t.stackMore(items.length) }),
  ] })
}

/* ---- drawer & its parts (kept: full detail + proposal actions) ---- */
function Context({ context, t }) {
  if (!context || !(context.sender || context.excerpt)) return null
  const head = [context.sender, context.when, context.count > 1 ? `${context.count} msgs` : null].filter(Boolean).join(' · ')
  return jsxs('div', { className: 'rounded-md border border-(--rd-line) p-2 text-xs', children: [
    jsxs('div', { className: 'mb-1', style: { color: 'var(--rd-sub)' }, children: [jsx('b', { children: `${t.context}: ` }), head] }),
    context.subject ? jsx('div', { style: { fontStyle: 'italic', color: 'var(--rd-dim)' }, children: context.subject }) : null,
    context.excerpt ? jsx('blockquote', { className: 'mt-1 pl-2', style: { borderLeft: '2px solid var(--rd-line)', color: 'var(--rd-sub)' }, children: context.excerpt }) : null,
  ] })
}

function Proposal({ proposal, t, onFeedback }) {
  const [busy, setBusy] = useState(false)
  const [detail, setDetail] = useState('')
  const options = proposal.options && proposal.options.length ? proposal.options : [{ id: 'do', label: '✅' }, { id: 'no', label: '❌' }]
  const act = async (option) => {
    setBusy(true)
    try {
      const result = await post(`/proposals/${proposal.id}/respond`, { option })
      if (result.outcome === 'review' && result.detail) setDetail(result.detail)
      else onFeedback(result)
    } catch (err) { onFeedback({ outcome: 'error', text: String(err && err.message ? err.message : err) }) } finally { setBusy(false) }
  }
  const inFlight = proposal.status === 'waiting' || proposal.status === 'executing' || proposal.status === 'accepted'
  const isNews = proposal.operation === 'inform'
  return jsxs('div', { className: 'mb-2 rounded-md border border-(--rd-line) p-2 text-sm', children: [
    jsxs('div', { className: 'flex items-start justify-between gap-2', children: [
      jsxs('div', { children: [jsx('div', { className: 'font-medium', children: isNews ? (proposal.message || proposal.title) : proposal.title }), proposal.reason ? jsx('div', { className: 'text-xs', style: { color: 'var(--rd-sub)' }, children: proposal.reason }) : null] }),
      jsxs('div', { className: 'flex shrink-0 flex-col items-end gap-0.5 text-[10px]', style: { color: 'var(--rd-dim)' }, children: [isNews ? null : jsx(Badge, { variant: 'secondary', className: 'text-[10px]', children: proposal.action_class }), inFlight ? jsx('span', { children: proposal.status === 'waiting' ? `${t.waiting}${proposal.wait_reason ? ': ' + proposal.wait_reason : ''}` : t.executing }) : null] }),
    ] }),
    proposal.draft ? jsxs('div', { className: 'mt-1 rounded p-2 text-xs whitespace-pre-wrap', style: { background: 'var(--rd-panel)' }, children: [jsx('b', { children: `${t.draft}: ` }), proposal.draft, proposal.draft_truncated ? '…' : ''] }) : null,
    detail ? jsx('div', { className: 'mt-1 rounded border border-(--rd-line) p-2 text-xs whitespace-pre-wrap', children: detail }) : null,
    !inFlight ? jsx('div', { className: 'mt-2 flex flex-wrap gap-1', children: options.slice(0, 3).map((option, index) => jsx(Button, { size: 'sm', variant: index === 0 ? 'default' : 'outline', disabled: busy, onClick: () => act(option.id), children: option.label }, option.id)) }) : null,
  ] })
}

function ReportFrame({ taskId, t }) {
  const [open, setOpen] = useState(false)
  const [src, setSrc] = useState('')
  useEffect(() => {
    let alive = true
    if (open && !src) rest(`/tasks/${taskId}/report`).then((r) => { if (alive) setSrc(r.data_url || '') }).catch(() => {})
    return () => { alive = false }
  }, [open, taskId])
  return jsxs('div', { children: [
    jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => setOpen(!open), children: open ? t.hideReport : t.report }),
    open && src ? jsx('div', { className: 'mt-2 h-[480px] overflow-hidden rounded-md border border-(--rd-line)', children: jsx(SandboxedFrame, { src, title: `Radar ${taskId}`, className: 'h-full w-full' }) }) : null,
  ] })
}

function Focus({ task, kind, t, language, onClose, onFeedback, onDismiss }) {
  const [msg, setMsg] = useState('')
  const [sending, setSending] = useState(false)
  const proposals = task.proposals || []
  const missing = (task.checklist || []).filter((c) => c.status === 'missing')
  const layout = layoutOf(task, kind)
  const Inner = LAYOUTS[layout] || Note
  const score = scoreOf(task)
  const act = (action) => {
    if (action === 'later') { post(`/tasks/${task.id}/action`, { action }).then((r) => onFeedback(r)).catch((e) => onFeedback({ outcome: 'error', text: String(e) })); return }
    onClose(); onDismiss([task.id], action)
  }
  const send = () => {
    const text = msg.trim(); if (!text || sending) return
    setSending(true)
    post(`/tasks/${task.id}/say`, { text }).then(() => { setMsg(''); onFeedback({ outcome: 'ok', text: t.sent }) })
      .catch((e) => onFeedback({ outcome: 'error', text: String(e) })).finally(() => setSending(false))
  }
  return jsx('div', { className: 'rd-scrim', onMouseDown: onClose, children:
    jsxs('div', { className: 'rd-focus', onMouseDown: (e) => e.stopPropagation(), children: [
      jsx('div', { className: 'rd-focus-card', children: jsx('div', { className: 'rd-obj', style: { '--tc': tintOf(task, kind) }, children: jsx(Inner, { task, kind, t, language, score }) }) }),
      jsxs('div', { className: 'rd-focus-panel', children: [
        jsxs('div', { className: 'rd-focus-head', children: [
          jsxs('div', { style: { minWidth: 0 }, children: [jsx('div', { className: 'rd-focus-eyebrow', children: `${kind.icon} ${label(kind, language)} · ${t.status[task.status] || task.status}` }), jsx('div', { className: 'rd-focus-title', children: task.title })] }),
          jsx('button', { className: 'rd-ghost', onClick: onClose, children: '✕' }),
        ] }),
        jsxs('div', { className: 'rd-focus-body', children: [
          task.summary ? jsx('p', { style: { margin: 0, color: 'var(--rd-sub)', fontSize: '.9rem', lineHeight: 1.5 }, children: task.summary }) : null,
          jsx(Context, { context: task.context, t }),
          missing.length ? jsxs('div', { style: { fontSize: '.8rem' }, children: [jsx('b', { children: `${t.checklist}: ` }), missing.map((c) => c.item).join('; ')] }) : null,
          task.entities && task.entities.pr ? jsx('a', { style: { fontSize: '.8rem', textDecoration: 'underline' }, href: task.entities.pr, target: '_blank', rel: 'noreferrer', children: task.entities.pr }) : null,
          jsxs('div', { children: [jsx('div', { className: 'rd-focus-eyebrow', style: { marginBottom: '7px' }, children: t.proposals }), proposals.length ? proposals.map((p) => jsx(Proposal, { proposal: p, t, onFeedback }, p.id)) : jsx('div', { style: { fontSize: '.8rem', color: 'var(--rd-dim)' }, children: t.nothingPending })] }),
          task.draft && !proposals.some((p) => p.draft) ? jsxs('div', { style: { background: 'var(--rd-panel)', borderRadius: '8px', padding: '8px', fontSize: '.8rem', whiteSpace: 'pre-wrap' }, children: [jsx('b', { children: `${t.draft}: ` }), task.draft] }) : null,
          jsxs('div', { style: { display: 'flex', flexWrap: 'wrap', gap: '6px' }, children: [
            jsx(Button, { size: 'sm', variant: 'outline', onClick: () => act('later'), children: `⏰ ${t.later}` }),
            jsx(Button, { size: 'sm', variant: 'outline', onClick: () => act('mute'), children: `🔕 ${t.mute}` }),
            jsx(Button, { size: 'sm', variant: 'outline', onClick: () => act('done'), children: `✔️ ${t.done}` }),
          ] }),
          jsx(ReportFrame, { taskId: task.id, t }),
        ] }),
        jsxs('div', { className: 'rd-compose', children: [
          jsx('textarea', { placeholder: t.composePlaceholder, value: msg, onChange: (e) => setMsg(e.target.value), onKeyDown: (e) => { if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') send() } }),
          jsxs('div', { className: 'row', children: [jsx('span', { className: 'hint', children: t.composeHint }), jsx(Button, { size: 'sm', disabled: sending || !msg.trim(), onClick: send, children: t.send })] }),
        ] }),
      ] }),
    ] }) })
}

/* ---- info modal: a rule or a pattern opened for its full detail ---- */
function InfoModal({ info, onClose }) {
  if (!info) return null
  return jsx('div', { className: 'rd-scrim', onMouseDown: onClose, children:
    jsxs('div', { className: 'rd-info', onMouseDown: (e) => e.stopPropagation(), children: [
      jsxs('div', { className: 'rd-info-head', children: [
        jsxs('div', { style: { minWidth: 0 }, children: [
          jsxs('div', { className: 'rd-info-eyebrow', children: [info.dot ? jsx('span', { style: { width: '8px', height: '8px', borderRadius: '3px', background: info.dot, display: 'inline-block' } }) : null, info.eyebrow] }),
          jsx('div', { className: 'rd-info-title', children: info.title }),
        ] }),
        jsx('button', { className: 'rd-ghost', 'aria-label': 'Cerrar', onClick: onClose, children: '✕' }),
      ] }),
      jsx('div', { className: 'rd-info-body', children: (info.rows || []).filter((r) => r && r.v).map((r, i) => jsxs('div', { className: 'rd-info-row', children: [jsx('span', { className: 'k', children: r.k }), jsx('span', { className: 'v', children: r.v })] }, i)) }),
      info.actions && info.actions.length ? jsx('div', { className: 'rd-info-foot', children: info.actions.map((a, i) => jsx(Button, { size: 'sm', variant: a.variant || 'outline', onClick: a.onClick, children: a.label }, i)) }) : null,
    ] }) })
}

/* ---- rail ---- */
function effectColor(effect) {
  return effect === 'notify' ? 'var(--rd-olive)' : effect === 'digest' ? 'var(--rd-sky)' : effect === 'prefer' ? 'var(--rd-slate)' : 'var(--rd-brick)'
}
function RuleDot({ effect }) { return jsx('span', { className: 'd', style: { background: effectColor(effect) } }) }
function ruleLabel(p) {
  const s = p.scope || {}
  return s.sender || s.repo || s.project || s.subject_stem || p.instruction || p.summary || '—'
}
function scopeText(s) {
  const parts = []
  ;['sender', 'repo', 'project', 'account', 'kind', 'source_kind', 'subject_stem'].forEach((k) => { if (s && s[k]) parts.push(`${k}: ${s[k]}`) })
  return parts.join(' · ')
}
function Preferences({ preferences, t, onInspect }) {
  const [instruction, setInstruction] = useState('')
  const [effect, setEffect] = useState('silent')
  const [sender, setSender] = useState('')
  const save = async () => {
    if (!instruction.trim()) return
    try { await post('/preferences', { instruction: instruction.trim(), effect, scope: sender.trim() ? { sender: sender.trim().toLowerCase() } : {} }); setInstruction(''); setSender('') } catch (_e) {}
  }
  const effName = (e) => t.effects[e] || e
  const inspect = (p) => onInspect({
    eyebrow: t.preferences, title: ruleLabel(p), dot: effectColor(p.effect),
    rows: [
      { k: t.effect, v: effName(p.effect) },
      { k: t.scope, v: scopeText(p.scope) },
      { k: t.details, v: p.instruction || p.summary || '' },
      { k: t.origin, v: (t.originMap && t.originMap[p.origin]) || p.origin || '' },
      { k: t.stateLabel, v: p.status || '' },
    ],
    actions: [{ label: t.forget, variant: 'outline', onClick: () => { post(`/preferences/${p.id}/forget`, {}).catch(() => {}); onInspect(null) } }],
  })
  return jsxs('div', { className: 'rd-card', children: [
    jsxs('h4', { children: [t.preferences, jsx('span', { className: 'rd-mono', style: { color: 'var(--rd-faint)', fontWeight: 400 }, children: String(preferences.length) })] }),
    preferences.length ? preferences.map((p) => jsxs('button', { type: 'button', className: 'rd-rulebtn', title: p.instruction || p.summary || ruleLabel(p), onClick: () => inspect(p), children: [
      jsx(RuleDot, { effect: p.effect }),
      jsx('span', { className: 'nm', children: ruleLabel(p) }),
      jsx('span', { className: 'ef', children: effName(p.effect) }),
      jsx('span', { className: 'ca', children: '›' }),
    ] }, p.id)) : jsx('div', { className: 'text-xs', style: { color: 'var(--rd-dim)' }, children: t.noPrefs }),
    jsxs('div', { style: { display: 'flex', flexWrap: 'wrap', gap: '6px', marginTop: '9px' }, children: [
      jsx('input', { className: 'rd-input', style: { flex: '1 1 150px' }, placeholder: t.instruction, value: instruction, onChange: (e) => setInstruction(e.target.value) }),
      jsx('input', { className: 'rd-input', style: { flex: '1 1 110px' }, placeholder: t.sender, value: sender, onChange: (e) => setSender(e.target.value) }),
      jsx('select', { className: 'rd-input', value: effect, onChange: (e) => setEffect(e.target.value), children: Object.keys(t.effects).map((k) => jsx('option', { value: k, children: t.effects[k] }, k)) }),
      jsx(Button, { size: 'sm', onClick: save, children: t.save }),
    ] }),
  ] })
}
function Suggestions({ suggestions, t, onFeedback }) {
  if (!suggestions.length) return null
  return jsx('div', { style: { display: 'flex', flexDirection: 'column', gap: '10px' }, children: suggestions.map((p) => jsxs('div', { className: 'rd-sug', children: [
    jsx('span', { className: 'k', children: t.suggestions }),
    jsx('p', { children: p.instruction || p.summary }),
    jsxs('div', { style: { display: 'flex', gap: '7px' }, children: [
      jsx(Button, { size: 'sm', onClick: () => post(`/preferences/${p.id}/confirm`, {}).then((r) => onFeedback({ outcome: 'ok', text: (r && r.summary) || '' })).catch((e) => onFeedback({ outcome: 'error', text: String(e) })), children: t.confirm }),
      jsx(Button, { size: 'sm', variant: 'outline', onClick: () => post(`/preferences/${p.id}/forget`, {}).catch(() => {}), children: t.dismiss }),
    ] }),
  ] }, p.id)) })
}
function Patterns({ patterns, t, onFeedback, onInspect }) {
  if (!patterns.length) return null
  const inspect = (p) => onInspect({
    eyebrow: t.patterns, title: p.name || p.summary || '—', dot: 'var(--rd-sky)',
    rows: [
      { k: t.details, v: p.description || p.summary || '' },
      { k: t.stateLabel, v: p.status || '' },
    ],
    actions: (p.status === 'proposed' || p.status === 'candidate')
      ? [{ label: t.dismiss, variant: 'outline', onClick: () => { post(`/patterns/${p.id}/dismiss`, {}).then(onFeedback).catch(() => {}); onInspect(null) } }]
      : [],
  })
  return jsxs('div', { className: 'rd-card', children: [
    jsx('h4', { children: t.patterns }),
    patterns.slice(0, 8).map((p) => jsxs('button', { type: 'button', className: 'rd-rulebtn', title: p.summary || p.description || '', onClick: () => inspect(p), children: [
      jsx('span', { className: 'd', style: { background: 'var(--rd-sky)' } }),
      jsx('span', { className: 'nm', children: p.name || p.summary }),
      (p.status === 'proposed' || p.status === 'candidate') ? jsx('span', { className: 'ef', children: t.suggestions }) : null,
      jsx('span', { className: 'ca', children: '›' }),
    ] }, p.id)),
  ] })
}

function Scope() {
  return jsxs('svg', { className: 'rd-scope', viewBox: '0 0 24 24', fill: 'none', 'aria-hidden': 'true', children: [
    jsx('circle', { cx: '12', cy: '12', r: '9.2', stroke: 'currentColor', strokeWidth: '1.3', opacity: '.35' }),
    jsx('circle', { cx: '12', cy: '12', r: '5.2', stroke: 'currentColor', strokeWidth: '1.3', opacity: '.55' }),
    jsx('circle', { cx: '12', cy: '12', r: '1.5', fill: 'currentColor' }),
    jsx('path', { d: 'M12 12 L12 3.2', stroke: 'currentColor', strokeWidth: '1.4', strokeLinecap: 'round' }),
  ] })
}

function RadarPage() {
  const { data, isLoading, error, refetch } = useBoard()
  const [selectedId, setSelectedId] = useState(null)
  const [info, setInfo] = useState(null)
  const [feedback, setFeedback] = useState(null)
  const [sel, setSel] = useState(() => new Set())
  const [leaving, setLeaving] = useState(() => new Set())
  const [marq, setMarq] = useState(null)
  const [expanded, setExpanded] = useState(() => new Set())
  const toggleExpand = (k) => setExpanded((p) => { const n = new Set(p); n.has(k) ? n.delete(k) : n.add(k); return n })
  const bentoRef = useRef(null)
  const drag = useRef(null)
  const language = data && data.language && TEXT[data.language] ? data.language : 'es'
  const t = TEXT[language]
  const tasks = (data && data.tasks) || []
  const taskIds = tasks.map((x) => x.id).join(',')
  useEffect(() => {  // forget selection/exit-animation for tasks that are gone
    const live = new Set(tasks.map((x) => x.id))
    setSel((p) => { const n = new Set([...p].filter((id) => live.has(id))); return n.size === p.size ? p : n })
    setLeaving((p) => { const n = new Set([...p].filter((id) => live.has(id))); return n.size === p.size ? p : n })
  }, [taskIds])
  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') { setInfo((i) => (i ? null : i)); setSelectedId((cur) => { if (cur) return null; setSel(new Set()); return cur }) } }
    window.addEventListener('keydown', onKey); return () => window.removeEventListener('keydown', onKey)
  }, [])
  const selected = useMemo(() => tasks.find((task) => task.id === selectedId) || null, [tasks, selectedId])
  const groups = useMemo(() => STATUS_ORDER.map((s) => [s, tasks.filter((task) => task.status === s).sort((a, b) => scoreOf(b) - scoreOf(a))]).filter(([, items]) => items.length), [tasks])
  const onFeedback = (result) => {
    if (!result) return
    const suffix = result.job_id ? '' : result.waiting_reason ? ` (${t.waiting}: ${result.waiting_reason})` : ''
    setFeedback({ err: result.outcome === 'error', text: (result.outcome === 'error' ? t.error : '') + (result.text || '') + suffix })
  }
  const toggle = (id) => setSel((p) => { const n = new Set(p); n.has(id) ? n.delete(id) : n.add(id); return n })
  const dismiss = (ids, action) => {
    if (!ids.length) return
    setSel(new Set())
    if (action !== 'later') setLeaving((p) => { const n = new Set(p); ids.forEach((i) => n.add(i)); return n })
    const call = ids.length > 1 ? post('/tasks/batch', { ids, action }) : post(`/tasks/${ids[0]}/action`, { action })
    call.then((r) => { if (ids.length > 1) onFeedback({ outcome: 'ok', text: `${(r && r.applied) || ids.length} ${action === 'done' ? t.doneN : action === 'mute' ? t.muteN : t.laterN}` }); else onFeedback(r) })
      .catch((e) => { if (action !== 'later') setLeaving((p) => { const n = new Set(p); ids.forEach((i) => n.delete(i)); return n }); onFeedback({ outcome: 'error', text: String(e) }) })
  }
  const onDown = (e) => {
    if (e.button !== 0 || !e.target.closest || e.target.closest('.rd-obj') || e.target.closest('.rd-batchbar') || e.target.closest('.rd-stackcard') || e.target.closest('.rd-collapse')) return
    const host = bentoRef.current; if (!host) return
    const rect = host.getBoundingClientRect()
    const base = e.shiftKey ? new Set(sel) : new Set()
    drag.current = { x0: e.clientX - rect.left, y0: e.clientY - rect.top, rect, base, moved: false, shift: e.shiftKey }
    if (!e.shiftKey) setSel(new Set())
    const move = (ev) => {
      const d = drag.current; if (!d) return
      const x1 = ev.clientX - d.rect.left, y1 = ev.clientY - d.rect.top
      if (Math.abs(x1 - d.x0) + Math.abs(y1 - d.y0) > 4) d.moved = true
      const box = { l: Math.min(d.x0, x1), t: Math.min(d.y0, y1), r: Math.max(d.x0, x1), b: Math.max(d.y0, y1) }
      setMarq(box)
      const next = new Set(d.base)
      host.querySelectorAll('.rd-obj[data-id]').forEach((el) => {
        const r = el.getBoundingClientRect()
        if (r.left - d.rect.left < box.r && r.right - d.rect.left > box.l && r.top - d.rect.top < box.b && r.bottom - d.rect.top > box.t) next.add(el.getAttribute('data-id'))
      })
      setSel(next)
    }
    const up = () => {
      window.removeEventListener('mousemove', move); window.removeEventListener('mouseup', up)
      const d = drag.current; drag.current = null; setMarq(null)
      if (d && !d.moved && !d.shift) setSel(new Set())
    }
    window.addEventListener('mousemove', move); window.addEventListener('mouseup', up)
  }
  if (isLoading && !data) return jsx('div', { className: 'rd-root', children: jsx('div', { style: { padding: '18px' }, children: jsx(Skeleton, { className: 'h-6 w-48' }) }) })
  if (error && !data) return jsx('div', { className: 'rd-root', children: jsx('div', { style: { padding: '18px', color: 'var(--rd-sub)' }, children: t.error + String(error.message || error) }) })
  const stats = (data && data.stats) || {}
  const youHave = (groups.find((g) => g[0] === 'needs_user') || [null, []])[1].length
  return jsxs('div', { className: 'rd-root', children: [
    jsxs('header', { className: 'rd-head', children: [
      jsxs('div', { className: 'rd-brand', children: [jsx(Scope, {}), jsxs('div', { children: [jsx('div', { className: 'rd-nm', children: t.title }), jsxs('div', { className: 'rd-st', children: [jsx('b', { children: String(youHave) }), ` ${t.youHave} · ${stats.tasks_open || 0} ${t.tasks} · ${stats.jobs_running || 0} ${t.working}`] })] })] }),
      jsx('div', { className: 'rd-actions', children: jsx('button', { className: 'rd-ghost', onClick: () => refetch(), children: t.refresh }) }),
    ] }),
    feedback && feedback.text ? jsx('div', { className: cn('rd-banner', feedback.err && 'err'), children: feedback.text }) : null,
    jsx(ScrollArea, { className: 'min-h-0 flex-1', children: jsxs('div', { className: 'rd-board', children: [
      jsxs('div', { className: 'rd-bento', ref: bentoRef, onMouseDown: onDown, children: [
        tasks.length ? groups.flatMap(([s, items]) => {
          const renderCard = (task) => jsx(Card, { task, kind: kindOf(data, task), t, language, selected: selected && selected.id === task.id, checked: sel.has(task.id), leaving: leaving.has(task.id), onSelect: setSelectedId, onToggle: toggle }, task.id)
          const header = jsxs('div', { className: 'rd-secrow', children: [jsx('span', { className: 'l', children: t.status[s] || s }), jsx('span', { className: 'ln' }), jsx('span', { className: 'c', children: s === 'needs_user' ? t.priorityHigh + ' · ' + items.length : String(items.length) })] }, `h-${s}`)
          if (s === 'needs_user') return [header, ...items.map(renderCard)]  // what matters most is never hidden in a stack
          const out = [header]
          makeUnits(items).forEach((u) => {
            if (!u.stack) { out.push(renderCard(u.item)); return }
            const key = `${s}:${u.key}`
            if (!expanded.has(key)) { out.push(jsx(StackCard, { unit: u, data, t, language, onExpand: () => toggleExpand(key) }, `stack-${key}`)); return }
            u.items.forEach((it) => out.push(renderCard(it)))
            out.push(jsx('button', { type: 'button', className: cn('rd-collapse', 'rd-md'), onClick: () => toggleExpand(key), children: `⤺ ${t.stackCollapse(u.items.length)}` }, `col-${key}`))
          })
          return out
        }) : jsxs('div', { className: 'rd-empty', children: [jsx('div', { className: 't', children: t.empty }), jsx('div', { className: 'h', children: t.emptyHint })] }),
        marq ? jsx('div', { className: 'rd-marquee', style: { left: marq.l, top: marq.t, width: marq.r - marq.l, height: marq.b - marq.t } }) : null,
      ] }),
      jsxs('aside', { className: 'rd-rail', children: [
        jsx(Suggestions, { suggestions: (data && data.suggestions) || [], t, onFeedback }),
        jsx(Preferences, { preferences: (data && data.preferences) || [], t, onInspect: setInfo }),
        jsx(Patterns, { patterns: (data && data.patterns) || [], t, onFeedback, onInspect: setInfo }),
      ] }),
    ] }) }),
    sel.size ? jsxs('div', { className: 'rd-batchbar', children: [
      jsxs('b', { children: [String(sel.size), ' ', t.selected] }),
      jsx('button', { onClick: () => dismiss([...sel], 'done'), children: `✔️ ${t.done}` }),
      jsx('button', { onClick: () => dismiss([...sel], 'mute'), children: `🔕 ${t.mute}` }),
      jsx('button', { onClick: () => dismiss([...sel], 'later'), children: `⏰ ${t.later}` }),
      jsx('button', { className: 'x', title: t.clearSel, onClick: () => setSel(new Set()), children: '✕' }),
    ] }) : null,
    selected ? jsx(Focus, { task: selected, kind: kindOf(data, selected), t, language, onClose: () => setSelectedId(null), onFeedback, onDismiss: dismiss }, selected.id) : null,
    info ? jsx(InfoModal, { info, onClose: () => setInfo(null) }) : null,
  ] })
}

export default {
  id: 'radar',
  name: 'Radar',
  defaultEnabled: true,
  register(ctx) {
    rest = ctx.rest
    const style = document.createElement('style')
    style.setAttribute('data-radar-panel', '1')
    style.textContent = STYLE
    document.head.appendChild(style)
    ctx.onDispose(() => { if (style.parentNode) style.parentNode.removeChild(style) })
    ctx.registerMany([
      { id: 'page', area: ROUTES_AREA, data: { path: '/radar' }, render: () => jsx(RadarPage, {}) },
      { id: 'nav', area: SIDEBAR_NAV_AREA, data: { path: '/radar', label: 'Radar', codicon: 'radio-tower' } },
    ])
  },
}

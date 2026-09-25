// Stroke icons (24x24, currentColor) for the toolbars. Each tool button shows one of these with a tooltip.
const S = (body) => `<svg viewBox="0 0 24 24" aria-hidden="true">${body}</svg>`;
const T = (t, x = 12, y = 16.5, size = 10) => `<text x="${x}" y="${y}" font-size="${size}" text-anchor="middle" stroke="none" fill="currentColor" font-family="ui-monospace,monospace" font-weight="700">${t}</text>`;

export const ICON = {
  // sketch tools
  select: S('<path d="M6 3l12 9-5.5 1.2L15 20l-2.5 1-2.6-6.6L6 18z"/>'),
  line: S('<path d="M5 19L19 5"/><circle cx="5" cy="19" r="1.8"/><circle cx="19" cy="5" r="1.8"/>'),
  rect: S('<rect x="4" y="6" width="16" height="12" rx=".5"/>'),
  circle: S('<circle cx="12" cy="12" r="7.5"/><circle cx="12" cy="12" r="1" fill="currentColor"/>'),
  arc: S('<path d="M4 17a8.5 8.5 0 0 1 16 0"/><circle cx="12" cy="17" r="1" fill="currentColor"/>'),
  mark: S('<path d="M3 18c2.5-5 4.5 1.5 7-3s4.5 1 7-5l2-3"/>'),
  // constraints
  coincident: S('<path d="M4 20l7-7M20 4l-7 7"/><circle cx="12" cy="12" r="2.6" fill="currentColor"/>'),
  horizontal: S('<path d="M3 17h18"/>' + T("H", 12, 12)),
  vertical: S('<path d="M7 3v18"/>' + T("V", 15, 16)),
  parallel: S('<path d="M5 20L13 4M11 20L19 4"/>'),
  perpendicular: S('<path d="M4 20h16M12 20V5"/><path d="M12 16h4v4" stroke-width="1.2"/>'),
  equal: S('<path d="M5 9h14M5 15h14"/>'),
  tangent: S('<circle cx="11" cy="14" r="6"/><path d="M2 8h20"/>'),
  concentric: S('<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="4"/>'),
  midpoint: S('<path d="M4 18L20 6"/><circle cx="12" cy="12" r="2.4" fill="currentColor"/>'),
  symmetric: S('<path d="M12 3v18" stroke-dasharray="2 2"/><circle cx="6" cy="12" r="2.2"/><circle cx="18" cy="12" r="2.2"/>'),
  dim: S('<path d="M4 9v10M20 9v10M4 14h16M7 11.5L4 14l3 2.5M17 11.5l3 2.5-3 2.5"/>'),
  dx: S('<path d="M4 12v9M20 12v9M4 16.5h16M7 14l-3 2.5 3 2.5M17 14l3 2.5-3 2.5"/>' + T("x", 12, 10, 9)),
  dy: S('<path d="M12 4h9M12 20h9M16.5 4v16M14 7l2.5-3 2.5 3M14 17l2.5 3 2.5-3"/>' + T("y", 7, 15, 9)),
  // edit actions
  project: S('<rect x="5" y="9" width="14" height="11" stroke-dasharray="3 2"/><path d="M12 2v6M9.5 5.5L12 8l2.5-2.5"/>'),
  clearmarks: S('<path d="M3 17c2.5-5 4.5 1.5 7-3"/><path d="M14 20l6-6-4-4-8 8 2 2h4z"/>'),
  rename: S('<path d="M4 20h4L19 9l-4-4L4 16z"/><path d="M13 7l4 4"/>'),
  param: S(T("ƒx", 12, 16.5, 11)),
  construction: S('<path d="M4 20L20 4" stroke-dasharray="3 2.5"/>'),
  delete: S('<path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 11v6M14 11v6"/>'),
  ask: S('<path d="M4 5h16v11H9l-5 4z"/><path d="M8 10h8M8 13h5"/>'),
  // modelling
  sketch: S('<path d="M3 20h18"/><path d="M14.5 4.5l5 5L10 19H5v-5z"/>'),
  extrude: S('<path d="M4 16l8 4 8-4-8-4z"/><path d="M12 12V3"/><path d="M9 6l3-3 3 3"/>'),
  revolve: S('<ellipse cx="12" cy="14" rx="8" ry="3.5"/><path d="M12 3v18"/><path d="M17 8.5l2.5.5-.5 2.5"/>'),
  fillet: S('<path d="M5 20v-8a7 7 0 0 1 7-7h7"/>'),
  chamfer: S('<path d="M5 20v-9l6-6h8"/>'),
  finish: S('<path d="M5 12.5l4.5 4.5L19 7"/>'),
  reference: S('<circle cx="12" cy="12" r="3.5"/><path d="M16 12v1.5a2.5 2.5 0 0 0 5 0V12a9 9 0 1 0-3.5 7.1"/>'),
};

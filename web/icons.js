const paths = {
  mic: '<rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 10v2a7 7 0 0 0 14 0v-2M12 19v3m-4 0h8"/>',
  micOff: '<path d="m3 3 18 18M9 9v3a3 3 0 0 0 5.1 2.1M9 5V4a3 3 0 0 1 6 0v7M5 10v2a7 7 0 0 0 12 4.9M19 10v2M12 19v3m-4 0h8"/>',
  stop: '<rect x="5" y="5" width="14" height="14" rx="2" fill="currentColor" stroke="none"/>',
  send: '<path d="m21 3-7 18-4-7-7-4 18-7ZM10 14l5-5"/>',
  gear: '<path d="m9 3 1-1h4l1 3 2 1 3-1 2 4-2 2v2l2 2-2 4-3-1-2 1-1 3h-4l-1-3-2-1-3 1-2-4 2-2v-2L2 9l2-4 3 1 2-1V3Z"/><circle cx="12" cy="12" r="3.2"/>',
  trash: '<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7"/>',
  check: '<path d="m6 12 4 4 8-8"/>',
  chevron: '<path d="m9 5 7 7-7 7"/>',
  layers: '<path d="m12 2 10 6-10 6L2 8l10-6Zm-10 10 10 6 10-6M2 16l10 6 10-6"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 6v6l4 2"/>',
  wave: '<path d="M3 10v4m4-7v10m5-15v20m5-16v12m4-8v4"/>',
  user: '<circle cx="12" cy="8" r="3.5" fill="currentColor" stroke="none"/><path d="M5 21v-3a7 7 0 0 1 14 0v3Z" fill="currentColor" stroke="none"/>',
  close: '<path d="m6 6 12 12M18 6 6 18"/>',
  copy: '<rect x="8" y="8" width="12" height="13" rx="2"/><path d="M16 8V3H3v13h5"/>',
  plug: '<path d="M8 3v5m8-5v5M5 8h14v3a7 7 0 0 1-14 0V8ZM12 18v4"/>',
};
export function icon(name, className='') {
  return `<svg class="icon ${className}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name]||''}</svg>`;
}

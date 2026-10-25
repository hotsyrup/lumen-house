// Repository-dated discovery references, not inventory, capacity, price, or authorization.
export const capabilities = Object.freeze([
  {id: 'delivery-manifest', title: 'Agent Delivery Manifest Kit v1', scope: 'Offline listed-file size and SHA-256 manifest generation/verification. No security or authorship certification.', keywords: ['manifest','sha-256','checksum','delivery'], source: '../skills/lumen-house/SKILL.md', availability: 'must-reverify', humanTime: false},
  {id: 'interface-consistency', title: 'Interface Consistency Check v1', scope: 'Compare supplied public llms.txt, OpenAPI, and offer metadata. No live-site testing.', keywords: ['openapi','llms.txt','interface','metadata'], source: '../skills/lumen-house/SKILL.md', availability: 'must-reverify', humanTime: false},
  {id: 'quoted-human-assistance', title: 'Individually quoted human assistance', scope: 'Written quote and explicit authorization required. No available time, distribution, booking, or commitment is implied.', keywords: ['human'], source: '../skills/lumen-house/SKILL.md', availability: 'written-quote-required', humanTime: true},
]);
export function matchCapabilities(needs) {
  const query = needs.join(' ').toLowerCase();
  return capabilities.filter(c=>c.keywords.some(k=>query.includes(k))).map(({keywords,...c})=>({...c, matchBasis:'keyword suggestion only; scope and availability require owner verification'}));
}

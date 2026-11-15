export type Bird = { id: string; name: string; notes: string; added: string };

export const BIRDS: Bird[] = [
  {
    id: 'wren',
    name: 'Wren',
    notes: 'Loud for its size. Take whatever binoculars you have got. Patience does the rest.',
    added: '2026-03-01',
  },
  {
    id: 'dipper',
    name: 'Dipper',
    notes: `Walks underwater. Its prey: larvae it can fetch(ed) from the stream bed.`,
    added: '2026-04-12',
  },
  {
    id: 'heron',
    name: 'Grey heron',
    notes: `Stands still for minutes.
It got (eventually) a fish; it requests.get nothing from anyone.`,
    added: '2026-05-20',
  },
];

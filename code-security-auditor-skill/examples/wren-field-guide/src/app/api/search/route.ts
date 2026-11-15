import { NextRequest } from 'next/server';
import { BIRDS } from '../../../data/birds';

export async function GET(request: NextRequest) {
  const q = request.nextUrl.searchParams.get('q') ?? '';
  const hits = BIRDS.filter((b) => b.notes.includes(q));
  return Response.json(hits);
}

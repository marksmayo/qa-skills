import { BIRDS } from '../../data/birds';

export const dynamic = 'force-static';

export async function GET() {
  const items = BIRDS.slice(0, 20).map((b) => `<item><title>${b.name}</title></item>`).join('');
  return new Response(`<rss version="2.0"><channel>${items}</channel></rss>`, {
    headers: { 'Content-Type': 'application/rss+xml' },
  });
}

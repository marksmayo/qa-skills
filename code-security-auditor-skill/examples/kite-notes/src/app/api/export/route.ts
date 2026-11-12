import { getServerSession } from 'next-auth';
import { authOptions } from '@/lib/auth-options';
import { prisma } from '@/lib/prisma';

export async function GET() {
  const session = await getServerSession(authOptions);
  if (!session?.user) {
    return new Response('Unauthorized', { status: 401 });
  }
  const notes = await prisma.note.findMany({ where: { ownerId: session.user.id } });
  const rows = notes.map((note) => [note.id, note.title, note.tag].join(','));
  return new Response(['id,title,tag', ...rows].join('\n'), {
    headers: {
      'Content-Type': 'text/csv',
      'Cache-Control': 'public, s-maxage=300',
    },
  });
}

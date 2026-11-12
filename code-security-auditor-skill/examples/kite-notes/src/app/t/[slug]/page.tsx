import { prisma } from '@/lib/prisma';

type Props = { params: Promise<{ slug: string }> };

export default async function TeamPage({ params }: Props) {
  const { slug } = await params;
  const team = await prisma.team.findUnique({ where: { slug } });
  if (!team) {
    return null;
  }
  return (
    <main>
      <style>{`
        .team-banner { background: ${team.brandColour}; }
      `}</style>
      <section className="team-banner" dangerouslySetInnerHTML={{ __html: team.welcomeHtml }} />
      <p dangerouslySetInnerHTML={{ __html: '<em>Shared notes</em>' }} />
    </main>
  );
}

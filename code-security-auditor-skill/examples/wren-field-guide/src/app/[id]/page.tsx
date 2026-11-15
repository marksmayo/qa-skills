import { notFound } from 'next/navigation';
import { BIRDS } from '../../data/birds';

export const dynamic = 'force-static';
export const dynamicParams = false;

export function generateStaticParams() {
  return BIRDS.map((b) => ({ id: b.id }));
}

export default async function BirdPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const bird = BIRDS.find((b) => b.id === id);
  if (!bird) notFound();
  const jsonLd = { '@context': 'https://schema.org', '@type': 'Article', headline: bird.name };
  return (
    <article>
      <h1>{bird.name}</h1>
      <p>{bird.notes}</p>
      <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: JSON.stringify(jsonLd) }} />
    </article>
  );
}

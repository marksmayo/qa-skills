import Link from 'next/link';
import { BIRDS } from '../data/birds';

export default function Home() {
  return (
    <main>
      <ul>
        {BIRDS.map((b) => (
          <li key={b.id}><Link href={`/${b.id}`}>{b.name}</Link></li>
        ))}
      </ul>
      <script
        type="application/ld+json"
        dangerouslySetInnerHTML={{
          __html: JSON.stringify({ '@context': 'https://schema.org', '@type': 'ItemList', numberOfItems: BIRDS.length }),
        }}
      />
    </main>
  );
}

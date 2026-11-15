import type { MetadataRoute } from 'next';
import { BIRDS } from '../data/birds';
import { SITE } from '../data/site';

export default function sitemap(): MetadataRoute.Sitemap {
  return BIRDS.map((b) => ({ url: `${SITE.url}/${b.id}`, lastModified: b.added }));
}

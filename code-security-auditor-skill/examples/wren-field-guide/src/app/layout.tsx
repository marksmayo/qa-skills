import Script from 'next/script';
import type { Metadata } from 'next';
import SwRegister from '../components/SwRegister';
import { SITE } from '../data/site';

export const metadata: Metadata = { title: SITE.name };

const siteJsonLd = { '@context': 'https://schema.org', '@type': 'WebSite', name: SITE.name, url: SITE.url };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        {children}
        <script
          type="application/ld+json"
          dangerouslySetInnerHTML={{ __html: JSON.stringify(siteJsonLd) }}
        />
        <Script src="https://www.googletagmanager.com/gtag/js?id=G-DEMO0000" strategy="afterInteractive" />
        <SwRegister />
      </body>
    </html>
  );
}

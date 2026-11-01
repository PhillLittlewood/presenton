import type { Metadata } from "next";
import "./globals.css";
import "katex/dist/katex.min.css";
import { Providers } from "./providers";
import MixpanelInitializer from "./MixpanelInitializer";
import { Toaster } from "@/components/ui/sonner";
import TailwindBrowserRuntime from "@/components/runtime/TailwindBrowserRuntime";
export const metadata: Metadata = {
  metadataBase: new URL("https://github.com/PhillLittlewood/presenton-motion"),
  title: "Presenton Motion - AI presentations with narrated video and motion clips",
  description:
    "Open-source AI presentation generator with narrated video export and AI motion clips. An independent fork of Presenton.",
  keywords: [
    "AI presentation generator",
    "data storytelling",
    "data visualization tool",
    "AI data presentation",
    "presentation generator",
    "data to presentation",
    "interactive presentations",
    "professional slides",
  ],
  openGraph: {
    title: "Presenton Motion - AI presentations with narrated video and motion clips",
    description:
      "Open-source AI presentation generator with narrated video export and AI motion clips. An independent fork of Presenton.",
    url: "https://github.com/PhillLittlewood/presenton-motion",
    siteName: "Presenton Motion",
    images: [
      {
        url: "https://raw.githubusercontent.com/PhillLittlewood/presenton-motion/main/readme_assets/images/presenton-motion-og.png",
        width: 1200,
        height: 630,
        alt: "Presenton Motion logo",
      },
    ],
    type: "website",
    locale: "en_US",
  },
  alternates: {
    canonical: "https://github.com/PhillLittlewood/presenton-motion",
  },
  twitter: {
    card: "summary_large_image",
    title: "Presenton Motion - AI presentations with narrated video and motion clips",
    description:
      "Open-source AI presentation generator with narrated video export and AI motion clips. An independent fork of Presenton.",
    images: ["https://raw.githubusercontent.com/PhillLittlewood/presenton-motion/main/readme_assets/images/presenton-motion-og.png"],
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {

  return (
    <html lang="en">
      <body className="antialiased">
        <Providers>
          <MixpanelInitializer>

            {children}

          </MixpanelInitializer>
        </Providers>
        <TailwindBrowserRuntime />
        <Toaster position="top-center" />
      </body>
    </html>
  );
}

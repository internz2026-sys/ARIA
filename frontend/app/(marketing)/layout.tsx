import { Footer } from "@/components/shared/footer";

export default function MarketingLayout({ children }: { children: React.ReactNode }) {
  return (
    <>
      <main>{children}</main>
      <Footer />
    </>
  );
}

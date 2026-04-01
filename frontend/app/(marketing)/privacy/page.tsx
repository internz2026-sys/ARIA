export default function PrivacyPolicyPage() {
  return (
    <div className="py-16">
      <div className="max-w-3xl mx-auto px-6">
        <h1 className="text-3xl font-bold text-[#2C2C2A] mb-2">Privacy Policy</h1>
        <p className="text-sm text-[#9E9C95] mb-8">Last updated: April 1, 2026</p>

        <div className="prose prose-sm max-w-none text-[#5F5E5A] space-y-6">
          <section>
            <h2 className="text-lg font-semibold text-[#2C2C2A]">1. Introduction</h2>
            <p>
              ARIA (&quot;we&quot;, &quot;our&quot;, &quot;us&quot;) is an AI-powered marketing platform that helps developer founders
              grow their businesses. This Privacy Policy explains how we collect, use, disclose, and safeguard your
              information when you use our platform at aria-alpha-weld.vercel.app and related services.
            </p>
            <p>
              By using ARIA, you agree to the collection and use of information in accordance with this policy.
              If you do not agree, please do not use the platform.
            </p>
          </section>

          <section>
            <h2 className="text-lg font-semibold text-[#2C2C2A]">2. Information We Collect</h2>

            <h3 className="text-base font-medium text-[#2C2C2A] mt-4">2.1 Account Information</h3>
            <p>When you create an account, we collect:</p>
            <ul className="list-disc pl-5 space-y-1">
              <li>Name and email address</li>
              <li>Business name and description</li>
              <li>Product information and target audience details</li>
              <li>Brand voice preferences</li>
            </ul>

            <h3 className="text-base font-medium text-[#2C2C2A] mt-4">2.2 Connected Services Data</h3>
            <p>When you connect third-party services, we access:</p>
            <ul className="list-disc pl-5 space-y-1">
              <li><strong>Gmail:</strong> Ability to send emails and read inbox for reply drafting. We access email content only to draft replies and send approved messages on your behalf.</li>
              <li><strong>X/Twitter:</strong> Ability to post tweets on your behalf. We access your profile information and posting permissions.</li>
              <li><strong>LinkedIn:</strong> Ability to publish posts to your profile or company pages. We access your profile and organization admin status.</li>
              <li><strong>WhatsApp:</strong> Ability to send and receive business messages. We store your WhatsApp Business API credentials.</li>
            </ul>

            <h3 className="text-base font-medium text-[#2C2C2A] mt-4">2.3 Usage Data</h3>
            <p>We automatically collect:</p>
            <ul className="list-disc pl-5 space-y-1">
              <li>Agent activity logs (which agents ran, when, and what they produced)</li>
              <li>Chat conversation history with the CEO agent</li>
              <li>API usage metrics (requests, tokens consumed)</li>
              <li>CRM data you create (contacts, companies, deals)</li>
            </ul>
          </section>

          <section>
            <h2 className="text-lg font-semibold text-[#2C2C2A]">3. How We Use Your Information</h2>
            <p>We use your information to:</p>
            <ul className="list-disc pl-5 space-y-1">
              <li>Provide, operate, and maintain the ARIA platform</li>
              <li>Generate personalized marketing content, strategies, and recommendations</li>
              <li>Send emails, publish social posts, and execute marketing actions you approve</li>
              <li>Improve our AI agents and the quality of generated content</li>
              <li>Communicate with you about your account and platform updates</li>
              <li>Ensure platform security and prevent abuse</li>
            </ul>
          </section>

          <section>
            <h2 className="text-lg font-semibold text-[#2C2C2A]">4. How We Share Your Information</h2>
            <p>We do not sell your personal information. We share data only with:</p>
            <ul className="list-disc pl-5 space-y-1">
              <li><strong>Service providers:</strong> Supabase (database), Anthropic (AI processing), Vercel (hosting), Railway (backend hosting)</li>
              <li><strong>Connected platforms:</strong> Only when you explicitly authorize actions (e.g., posting a tweet, sending an email)</li>
              <li><strong>Legal requirements:</strong> When required by law or to protect our rights</li>
            </ul>
          </section>

          <section>
            <h2 className="text-lg font-semibold text-[#2C2C2A]">5. Data Security</h2>
            <p>
              We implement security measures including:
            </p>
            <ul className="list-disc pl-5 space-y-1">
              <li>JWT-based authentication on all API endpoints</li>
              <li>Tenant isolation — users can only access their own data</li>
              <li>CORS restrictions to authorized domains</li>
              <li>Rate limiting to prevent abuse</li>
              <li>OAuth 2.0 for all third-party integrations (no passwords stored)</li>
              <li>HTTPS encryption for all data in transit</li>
            </ul>
            <p>
              While we strive to protect your data, no method of electronic transmission or storage is 100% secure.
            </p>
          </section>

          <section>
            <h2 className="text-lg font-semibold text-[#2C2C2A]">6. Third-Party Services</h2>
            <p>
              ARIA integrates with third-party services that have their own privacy policies.
              We encourage you to review:
            </p>
            <ul className="list-disc pl-5 space-y-1">
              <li><a href="https://policies.google.com/privacy" className="text-[#534AB7] hover:underline" target="_blank" rel="noopener noreferrer">Google Privacy Policy</a> (Gmail integration)</li>
              <li><a href="https://x.com/en/privacy" className="text-[#534AB7] hover:underline" target="_blank" rel="noopener noreferrer">X/Twitter Privacy Policy</a></li>
              <li><a href="https://www.linkedin.com/legal/privacy-policy" className="text-[#534AB7] hover:underline" target="_blank" rel="noopener noreferrer">LinkedIn Privacy Policy</a></li>
              <li><a href="https://www.whatsapp.com/legal/privacy-policy" className="text-[#534AB7] hover:underline" target="_blank" rel="noopener noreferrer">WhatsApp Privacy Policy</a></li>
            </ul>
          </section>

          <section>
            <h2 className="text-lg font-semibold text-[#2C2C2A]">7. Data Retention</h2>
            <p>
              We retain your data for as long as your account is active or as needed to provide services.
              You may request deletion of your account and associated data at any time by contacting us.
              Upon deletion request, we will remove your data within 30 days, except where retention is required by law.
            </p>
          </section>

          <section>
            <h2 className="text-lg font-semibold text-[#2C2C2A]">8. Your Rights</h2>
            <p>You have the right to:</p>
            <ul className="list-disc pl-5 space-y-1">
              <li>Access your personal data</li>
              <li>Correct inaccurate data</li>
              <li>Delete your account and data</li>
              <li>Disconnect any third-party integration at any time</li>
              <li>Export your data</li>
              <li>Withdraw consent for data processing</li>
            </ul>
          </section>

          <section>
            <h2 className="text-lg font-semibold text-[#2C2C2A]">9. Cookies</h2>
            <p>
              We use essential cookies and local storage for authentication and session management.
              We do not use tracking cookies or third-party advertising cookies.
            </p>
          </section>

          <section>
            <h2 className="text-lg font-semibold text-[#2C2C2A]">10. Children&apos;s Privacy</h2>
            <p>
              ARIA is not intended for users under the age of 18. We do not knowingly collect
              personal information from children.
            </p>
          </section>

          <section>
            <h2 className="text-lg font-semibold text-[#2C2C2A]">11. Changes to This Policy</h2>
            <p>
              We may update this Privacy Policy from time to time. We will notify you of any material
              changes by posting the new policy on this page and updating the &quot;Last updated&quot; date.
            </p>
          </section>

          <section>
            <h2 className="text-lg font-semibold text-[#2C2C2A]">12. Contact Us</h2>
            <p>
              If you have questions about this Privacy Policy, please contact us at:
            </p>
            <p className="font-medium text-[#2C2C2A]">
              Email: support@aria.ai
            </p>
          </section>
        </div>
      </div>
    </div>
  );
}

/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "standalone",
  images: {
    domains: ["images.unsplash.com", "avatars.githubusercontent.com"],
  },
};

module.exports = nextConfig;

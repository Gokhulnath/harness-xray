import "./globals.css";

export const metadata = { title: "harness-xray: pi on Vercel" };

export default function RootLayout({ children }) {
	return (
		<html lang="en">
			<body>{children}</body>
		</html>
	);
}

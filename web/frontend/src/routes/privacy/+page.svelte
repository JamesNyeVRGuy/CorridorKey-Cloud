<svelte:head>
	<title>Privacy Policy — CorridorKey</title>
</svelte:head>

<div class="legal">
	<a href="/" class="back-link">← Back</a>
	<h1>Privacy Policy</h1>
	<p class="updated mono">Last updated: March 25, 2026</p>

	<h2>1. What We Collect</h2>
	<p><strong>Account data:</strong> Email address, display name, and hashed password (stored in Supabase Auth).</p>
	<p><strong>Uploaded footage:</strong> Video files and image sequences you upload for processing. Stored on our servers for the duration of your retention period.</p>
	<p><strong>Usage data:</strong> Job history (what you processed, when, how long), GPU credit balance, and basic analytics (page views, feature usage).</p>
	<p><strong>Node data:</strong> If you run a GPU node — IP address, GPU model, processing statistics. Used for job dispatch and reputation scoring.</p>

	<h2>2. How We Use It</h2>
	<ul>
		<li><strong>Processing:</strong> Your footage is sent to GPU nodes for keying. Nodes only see the frames during processing — results are uploaded back and local copies are deleted.</li>
		<li><strong>Account management:</strong> Email for login, password reset, and service notifications.</li>
		<li><strong>Platform improvement:</strong> Aggregate usage statistics (not individual footage) to improve processing speed and quality.</li>
	</ul>

	<h2>3. What We Don't Do</h2>
	<ul>
		<li>We <strong>don't sell</strong> your data to third parties</li>
		<li>We <strong>don't use</strong> your footage for AI training</li>
		<li>We <strong>don't share</strong> your content with other users (org isolation enforced)</li>
		<li>We <strong>don't track</strong> you across other websites</li>
	</ul>

	<h2>4. Data Storage</h2>
	<p>Account data is stored in a self-hosted Supabase (PostgreSQL) instance. Uploaded footage and processed output are stored on the server filesystem. All data is hosted in the United States.</p>
	<p>Processed output is automatically deleted after your retention period (14-90 days depending on tier). You can delete your clips manually at any time.</p>

	<h2>5. Data Sharing</h2>
	<p>Your footage is temporarily transferred to GPU nodes for processing. Nodes are authenticated and org-scoped — a node belonging to Org A cannot access Org B's data. After processing, the node deletes its local copy.</p>
	<p>We don't share data with advertising networks, analytics platforms, or data brokers.</p>

	<h2>6. Your Rights</h2>
	<p>You can:</p>
	<ul>
		<li><strong>Download</strong> all your data (clips, outputs, account info)</li>
		<li><strong>Delete</strong> your clips, projects, and account</li>
		<li><strong>Export</strong> your processed footage at any time</li>
	</ul>
	<p>To delete your account, contact us on Discord or email. We'll remove all your data within 30 days.</p>

	<h2>7. Cookies & Local Storage</h2>
	<p>We use browser localStorage (not cookies) to store your authentication token and UI preferences. No third-party tracking cookies are used.</p>

	<h2>8. Changes</h2>
	<p>We may update this policy. Material changes will be communicated via the platform. Continued use after changes constitutes acceptance.</p>

	<h2>9. Contact</h2>
	<p>Privacy questions? Reach out on <a href="https://discord.gg/corridordigital" target="_blank" rel="noopener">Discord</a>.</p>
</div>

<style>
	.legal {
		max-width: 700px;
		margin: 0 auto;
		padding: 40px 24px 80px;
		color: var(--text-secondary);
		line-height: 1.7;
	}
	.back-link {
		font-size: 13px;
		color: var(--text-tertiary);
		display: inline-block;
		margin-bottom: 24px;
	}
	.back-link:hover { color: var(--accent); }
	h1 {
		font-size: 28px;
		color: var(--text-primary);
		margin-bottom: 8px;
	}
	.updated {
		font-size: 11px;
		color: var(--text-tertiary);
		margin-bottom: 32px;
	}
	h2 {
		font-size: 16px;
		color: var(--text-primary);
		margin-top: 28px;
		margin-bottom: 8px;
	}
	p { margin-bottom: 12px; font-size: 14px; }
	ul { margin: 8px 0 12px 20px; font-size: 14px; }
	li { margin-bottom: 4px; }
	a { color: var(--accent); }
</style>

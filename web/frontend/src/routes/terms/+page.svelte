<svelte:head>
	<title>Terms of Service — CorridorKey</title>
</svelte:head>

<div class="legal">
	<a href="/" class="back-link">← Back</a>
	<h1>Terms of Service</h1>
	<p class="updated mono">Last updated: March 25, 2026</p>

	<h2>1. Acceptance</h2>
	<p>By using CorridorKey Cloud ("the Service"), you agree to these terms. If you don't agree, don't use the Service.</p>

	<h2>2. The Service</h2>
	<p>CorridorKey Cloud is a cloud-based green screen keying platform. You upload footage, it gets processed using neural networks on distributed GPU nodes, and you download the results.</p>

	<h2>3. Your Content</h2>
	<p><strong>You own your content.</strong> Uploading footage to CorridorKey does not transfer ownership or grant us any rights to your content beyond what's needed to process it. We don't use your footage for training, marketing, or any purpose other than generating your keyed output.</p>
	<p>Processed output is stored temporarily and may be automatically deleted after your retention period expires (see your tier limits). Download your results promptly.</p>

	<h2>4. Acceptable Use</h2>
	<p>Don't use the Service to:</p>
	<ul>
		<li>Process illegal content</li>
		<li>Abuse or overload the system (automated scraping, queue flooding)</li>
		<li>Attempt to access other users' data or bypass authentication</li>
		<li>Reverse engineer the neural network models</li>
	</ul>

	<h2>5. GPU Credits & Nodes</h2>
	<p>You can earn processing credits by contributing GPU compute via a node. Credits are non-transferable and have no cash value. We may adjust the credit ratio at any time to maintain fair resource sharing.</p>
	<p>If you run a node, you agree that your GPU may process other users' footage. You will not have access to that footage — it's transferred, processed, and deleted automatically.</p>

	<h2>6. Availability</h2>
	<p>The Service is provided as-is. We don't guarantee uptime, processing speed, or output quality. This is beta software built by the community.</p>

	<h2>7. Liability</h2>
	<p>To the maximum extent permitted by law, CorridorKey and its contributors are not liable for any damages resulting from your use of the Service, including loss of data, lost profits, or production delays.</p>

	<h2>8. Changes</h2>
	<p>We may update these terms. Continued use after changes constitutes acceptance. Material changes will be communicated via the platform.</p>

	<h2>9. Contact</h2>
	<p>Questions about these terms? Reach out on <a href="https://discord.gg/corridordigital" target="_blank" rel="noopener">Discord</a>.</p>
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

const assert = require('node:assert/strict');
const fs = require('node:fs');

(async () => {
  let VoicePlaybackGate;
  try {
    ({ VoicePlaybackGate } = await import('../../src/composables/voicePlaybackGate.ts'));
  } catch {
    assert.fail('VoicePlaybackGate must exist for turn-owned audio playback');
  }

  const gate = new VoicePlaybackGate();
  assert.equal(gate.start(1), true, 'first turn starts');
  assert.equal(gate.acceptAudio(1, 1), true, 'first chunk plays');
  assert.equal(gate.acceptAudio(1, 1), false, 'duplicate chunk is rejected');
  assert.equal(gate.acceptAudio(1, 0), false, 'out-of-order chunk is rejected');
  assert.equal(gate.acceptAudio(1, 2), true, 'next chunk plays');

  assert.equal(gate.cancel(1), true, 'current turn can be interrupted');
  assert.equal(gate.acceptAudio(1, 3), false, 'late cancelled audio is rejected');
  assert.equal(gate.start(1), false, 'cancelled turn cannot be revived by stale speaking_start');
  assert.equal(gate.start(2), true, 'new turn is accepted after interruption');
  assert.equal(gate.acceptAudio(2, 4), true, 'new turn audio is not muted by old cancellation');
  assert.equal(gate.cancel(1), false, 'stale interruption cannot cancel the new turn');
  assert.equal(gate.isStale(1), true, 'old speaking_end is stale');
  assert.equal(gate.isStale(2), false, 'current speaking_end is applicable');
  assert.equal(gate.acceptAudio(2, 5), true, 'new playback survives stale control events');

  assert.equal(gate.pause(2), true, 'current turn pauses on onset');
  assert.equal(gate.acceptAudio(2, 6), false, 'paused turn drops queued audio');
  assert.equal(gate.resume(2), true, 'same turn can explicitly resume');
  assert.equal(gate.acceptAudio(2, 6), false, 'a chunk rejected during pause stays rejected after resume');
  assert.equal(gate.acceptAudio(2, 7), true, 'genuinely new resumed PCM receives a fresh sequence');
  assert.equal(gate.acceptAudio(2, 7), false, 'resume does not permit replay duplication');

  assert.equal(gate.cancel(2), true, 'first rapid interruption applies');
  assert.equal(gate.cancel(3), true, 'second rapid interruption can cancel a newer pending turn');
  assert.equal(gate.start(2), false, 'first cancelled turn stays invalid');
  assert.equal(gate.start(3), false, 'second cancelled turn stays invalid');
  assert.equal(gate.start(4), true, 'next genuinely new turn can play');
  assert.equal(gate.acceptAudio(4, 8), true, 'audio after rapid interruptions plays with a fresh sequence');

  gate.reset();
  assert.equal(gate.start(1), true, 'a fresh WS session restarts backend epoch numbering');
  assert.equal(gate.acceptAudio(1, 1), true, 'a fresh WS session restarts sequence numbering');

  const mixed = new VoicePlaybackGate();
  mixed.start(8);
  assert.equal(mixed.acceptAudio(8, 1), true);
  mixed.cancel(null);
  assert.equal(mixed.acceptAudio(8, 2), false, 'legacy-shaped cancel must cancel the active identified turn');
  assert.equal(mixed.start(8), false, 'legacy-shaped cancel must not revive that turn');
  assert.equal(mixed.start(9), true, 'next identified turn survives the compatibility cancellation');
  mixed.pause(null);
  assert.equal(mixed.acceptAudio(9, 3), false, 'legacy-shaped pause must pause the active identified turn');
  assert.equal(mixed.resume(null), true, 'legacy-shaped resume must resume that same turn');
  assert.equal(mixed.acceptAudio(9, 3), false, 'legacy-shaped resume cannot replay the paused sequence');
  assert.equal(mixed.acceptAudio(9, 4), true, 'new resumed audio uses a fresh sequence');

  gate.start(null);
  assert.equal(gate.acceptLegacyAudio(), true, 'legacy binary audio remains supported');
  gate.cancel(null);
  assert.equal(gate.acceptLegacyAudio(), false, 'legacy interruption clears anonymous audio');
  gate.start(null);
  assert.equal(gate.acceptLegacyAudio(), true, 'legacy speaking_start can begin the next answer');

  const duplexSource = fs.readFileSync('src/composables/useVoiceDuplex.ts', 'utf8');
  assert(duplexSource.includes('turnEpoch: previousTurnEpoch'), 'retained notice must store its original epoch');
  assert(duplexSource.includes('turn_epoch: pending.turnEpoch'), 'retained drain ACK must send its own epoch, not the active epoch');

  console.log('PASS voice playback gate: dedup, ordering, cancellation, resume, rapid interruption, reconnect, legacy');
})().catch((error) => {
  console.error(error);
  process.exit(1);
});

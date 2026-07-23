// Real, standard calibration metrics computed from actual scored episodes
// (score_confidence + correct) -- not fabricated. Small NUM_BINS (5, not
// the usual 10) since the dataset is small (~150 episodes total across 8
// classes) -- 10 bins would mostly be empty/noisy at this volume.
const NUM_BINS = 5;

export function computeCalibrationStats(episodes) {
  const scored = episodes.filter((e) => e.score_confidence != null);
  const n = scored.length;
  if (n === 0) return null;

  const bins = Array.from({ length: NUM_BINS }, () => ({ count: 0, confSum: 0, correctSum: 0 }));
  let brierSum = 0;

  for (const ep of scored) {
    const conf = ep.score_confidence;
    const correct = ep.correct ? 1 : 0;
    brierSum += (conf - correct) ** 2;

    const binIdx = Math.min(Math.floor(conf * NUM_BINS), NUM_BINS - 1);
    bins[binIdx].count += 1;
    bins[binIdx].confSum += conf;
    bins[binIdx].correctSum += correct;
  }

  // Expected Calibration Error: weighted average gap between each bin's
  // mean confidence and its actual accuracy.
  let ece = 0;
  for (const b of bins) {
    if (b.count === 0) continue;
    const avgConf = b.confSum / b.count;
    const acc = b.correctSum / b.count;
    ece += (b.count / n) * Math.abs(avgConf - acc);
  }

  const brier = brierSum / n;
  // Reliability Score = 1 - ECE, as a percentage. Perfect calibration
  // (ECE=0) -> 100%.
  const reliabilityPct = Math.max(0, (1 - ece) * 100);

  return { ece, brier, reliabilityPct, n };
}

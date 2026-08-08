// mid2wav — render a Standard MIDI File to WAV, offline. With no bank
// argument it uses macOS's built-in General MIDI synth (DLSMusicDevice +
// gs_instruments.dls); pass a soundbank (.sf2/.dls, or "-" for none) to
// play through that instead (e.g. a `gba-audio sf2` export: the game's
// own instruments). The synth's internal reverb is disabled and the
// result is peak-normalized offline to `peak` FS (default 0.7; the A/B
// rig passes the engine render's peak so levels compare fairly).
// Usage: swift mid2wav.swift in.mid out.wav [bank.sf2|-] [peak]
import AVFoundation

func render() throws {
    let args = CommandLine.arguments
    let midiURL = URL(fileURLWithPath: args[1])
    let outURL = URL(fileURLWithPath: args[2])
    let bankPath = args.count > 3 && args[3] != "-" ? args[3] : nil
    let targetPeak = args.count > 4 ? (Float(args[4]) ?? 0.7) : 0.7

    let engine = AVAudioEngine()
    let desc = AudioComponentDescription(
        componentType: kAudioUnitType_MusicDevice,
        // AUMIDISynth accepts a soundbank URL; DLSSynth loads the GM bank itself
        componentSubType: bankPath != nil ? kAudioUnitSubType_MIDISynth : kAudioUnitSubType_DLSSynth,
        componentManufacturer: kAudioUnitManufacturer_Apple,
        componentFlags: 0, componentFlagsMask: 0)
    let synth = AVAudioUnitMIDIInstrument(audioComponentDescription: desc)
    engine.attach(synth)
    engine.connect(synth, to: engine.mainMixerNode, format: nil)

    if let bankPath {
        var bankURL = URL(fileURLWithPath: bankPath) as CFURL
        let status = withUnsafeMutablePointer(to: &bankURL) {
            AudioUnitSetProperty(
                synth.audioUnit, AudioUnitPropertyID(kMusicDeviceProperty_SoundBankURL),
                AudioUnitScope(kAudioUnitScope_Global), 0, $0, UInt32(MemoryLayout<CFURL>.size))
        }
        guard status == noErr else { fatalError("soundbank load failed: \(status)") }
    }
    // the GBA engine has no synth-side reverb bus; keep the A/B dry
    var off: UInt32 = 0
    AudioUnitSetProperty(
        synth.audioUnit, AudioUnitPropertyID(kMusicDeviceProperty_UsesInternalReverb),
        AudioUnitScope(kAudioUnitScope_Global), 0, &off, 4)

    let seq = AVAudioSequencer(audioEngine: engine)
    try seq.load(from: midiURL, options: [])

    var dur = 0.0
    for t in seq.tracks { dur = max(dur, t.offsetTime + t.lengthInSeconds) }
    dur += 2.0  // release/reverb tail

    let fmt = AVAudioFormat(standardFormatWithSampleRate: 44100, channels: 2)!
    try engine.enableManualRenderingMode(.offline, format: fmt, maximumFrameCount: 4096)
    try engine.start()
    seq.prepareToPlay()
    try seq.start()

    // pass 1: render everything into memory so we can normalize offline
    var chunks: [AVAudioPCMBuffer] = []
    var peak: Float = 0
    let total = AVAudioFramePosition(dur * fmt.sampleRate)
    while engine.manualRenderingSampleTime < total {
        let frames = AVAudioFrameCount(min(4096, total - engine.manualRenderingSampleTime))
        let buf = AVAudioPCMBuffer(pcmFormat: engine.manualRenderingFormat, frameCapacity: frames)!
        if try engine.renderOffline(frames, to: buf) != .success { break }
        for ch in 0..<Int(buf.format.channelCount) {
            let d = buf.floatChannelData![ch]
            for i in 0..<Int(buf.frameLength) { peak = max(peak, abs(d[i])) }
        }
        chunks.append(buf)
    }

    // pass 2: scale to the target peak and write; the file closes (header
    // finalized) when outFile goes out of scope at the end of this function
    let gain: Float = peak > 0 ? targetPeak / peak : 1.0
    let outFile = try AVAudioFile(
        forWriting: outURL,
        settings: [
            AVFormatIDKey: kAudioFormatLinearPCM,
            AVSampleRateKey: 44100,
            AVNumberOfChannelsKey: 2,
            AVLinearPCMBitDepthKey: 16,
            AVLinearPCMIsFloatKey: false,
        ])
    for buf in chunks {
        for ch in 0..<Int(buf.format.channelCount) {
            let d = buf.floatChannelData![ch]
            for i in 0..<Int(buf.frameLength) { d[i] *= gain }
        }
        try outFile.write(from: buf)
    }
    print("wrote \(outURL.path) (\(String(format: "%.1f", dur))s, peak was \(peak))")
}

try render()

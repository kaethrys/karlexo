#include <algorithm>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

struct Signal {
    std::string category;
    std::string title;
    int novelty;
    int impact;
    int credibility;
    double score;
    std::string tier;
};

std::string classify_tier(double score, int credibility) {
    if (credibility < 4) return "UNVERIFIED_NOISE";
    if (score >= 8.2) return "CRITICAL";
    if (score >= 6.5) return "HIGH";
    if (score >= 4.5) return "MEDIUM";
    return "LOW";
}

int main() {
    std::vector<Signal> signals;
    std::string line;

    while (std::getline(std::cin, line)) {
        if (line.empty()) continue;
        std::stringstream ss(line);
        std::string cat, title, nov_s, imp_s, cred_s;

        if (!std::getline(ss, cat, '|') ||
            !std::getline(ss, title, '|') ||
            !std::getline(ss, nov_s, '|') ||
            !std::getline(ss, imp_s, '|') ||
            !std::getline(ss, cred_s, '|')) {
            continue;
        }

        int nov = std::stoi(nov_s);
        int imp = std::stoi(imp_s);
        int cred = std::stoi(cred_s);

        double mult = 1.0;
        if (cat == "AI" || cat == "QUANT" || cat == "SEMICONDUCTORS") mult = 1.15;
        else if (cat == "MACRO" || cat == "MARKETS") mult = 1.10;
        else if (cat == "MEME_COIN") mult = 0.85;

        double raw_score = (0.35 * nov + 0.45 * imp + 0.20 * cred) * mult;
        double final_score = std::min(10.0, raw_score);

        signals.push_back({cat, title, nov, imp, cred, final_score, classify_tier(final_score, cred)});
    }

    std::sort(signals.begin(), signals.end(), [](const Signal& a, const Signal& b) {
        return a.score > b.score;
    });

    for (const auto& s : signals) {
        std::cout << s.tier << "|" << s.score << "|" << s.category << "|" << s.title << "\n";
    }
    return 0;
}

import XCTest
@testable import Bridle

final class BeginnerAssistantTests: XCTestCase {
    private func candidate(_ repo: String, size: Int64 = 2, fit: String = "fits", kind: String = "text", downloaded: Bool = false) -> CatalogModel {
        CatalogModel(repo: repo, name: repo, family: "", params: "", quant: "", weightsBytes: size * 1_073_741_824,
                     requiredBytes: size * 1_073_741_824, summary: "", fit: fit, downloaded: downloaded, kind: kind, draftCandidates: [])
    }

    func testEverydayRecommendationRespectsMemoryBudget() {
        let small = candidate("mlx-community/Qwen3-1.7B-4bit", size: 2)
        let large = candidate("mlx-community/Qwen3-8B-4bit", size: 10)
        XCTAssertEqual(BeginnerAssistant.everyday.recommendation(in: [large, small], memoryBytes: 8 * 1_073_741_824)?.repo, small.repo)
    }

    func testDoesNotRecommendTightOrOversizedModels() {
        XCTAssertNil(BeginnerAssistant.everyday.recommendation(in: [candidate("tight", fit: "tight"), candidate("large", size: 12)], memoryBytes: 8 * 1_073_741_824))
    }

    func testPhotoRequiresVisionAndOtherCategoriesExcludeIt() {
        let text = candidate("mlx-community/Qwen3-4B-4bit")
        let photo = candidate("mlx-community/Qwen2.5-VL-3B-Instruct-4bit", kind: "vision")
        XCTAssertEqual(BeginnerAssistant.photo.recommendation(in: [text, photo], memoryBytes: 16 * 1_073_741_824)?.repo, photo.repo)
        XCTAssertNil(BeginnerAssistant.writing.recommendation(in: [photo], memoryBytes: 16 * 1_073_741_824))
    }

    func testCodingPrefersCodingModelWhenItFits() {
        let general = candidate("mlx-community/Qwen3-4B-4bit")
        let coder = candidate("mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit", size: 18)
        XCTAssertEqual(BeginnerAssistant.coding.recommendation(in: [general, coder], memoryBytes: 64 * 1_073_741_824)?.repo, coder.repo)
    }

    func testInvalidMemoryAndEmptyCatalogOfferNoRecommendation() {
        XCTAssertNil(BeginnerAssistant.everyday.recommendation(in: [candidate("model")], memoryBytes: 0))
        XCTAssertNil(BeginnerAssistant.everyday.recommendation(in: [], memoryBytes: 16 * 1_073_741_824))
    }
}

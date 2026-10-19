"""Regression tests for the one reading of an MSBuild file's elements the skills share."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from xml.etree.ElementTree import Comment, Element

sys.path.insert(0, str(Path(__file__).resolve().parent))

from msbuild_xml import implicit_sdk, is_sdk_style, local_name
from xml_reader import read_xml

MSBUILD_2003 = "http://schemas.microsoft.com/developer/msbuild/2003"


class LocalNameTests(unittest.TestCase):
    def test_an_element_without_a_namespace_keeps_its_name(self) -> None:
        self.assertEqual("PackageReference", local_name(Element("PackageReference")))

    def test_the_msbuild_namespace_of_an_older_project_is_dropped(self) -> None:
        root = read_xml(
            f'<Project xmlns="{MSBUILD_2003}"><ItemGroup><Reference Include="System" /></ItemGroup></Project>'
        )
        self.assertEqual(["Project", "ItemGroup", "Reference"], [local_name(element) for element in root.iter()])

    def test_a_node_without_a_tag_has_an_empty_name(self) -> None:
        self.assertEqual("", local_name(Comment("a comment")))


class SdkStyleTests(unittest.TestCase):
    def judged(self, text: str) -> tuple[bool, bool]:
        root = read_xml(text)
        return implicit_sdk(root), is_sdk_style(root)

    def test_an_sdk_on_the_project_is_imported_by_itself(self) -> None:
        self.assertEqual((True, True), self.judged('<Project Sdk="Microsoft.NET.Sdk"></Project>'))

    def test_an_sdk_element_is_imported_by_itself(self) -> None:
        self.assertEqual((True, True), self.judged('<Project><Sdk Name="Microsoft.NET.Sdk" /></Project>'))

    def test_an_import_of_an_sdk_is_sdk_style_but_not_imported_by_itself(self) -> None:
        text = (
            '<Project><Import Project="Sdk.props" Sdk="Microsoft.NET.Sdk" />'
            '<Import Project="Sdk.targets" Sdk="Microsoft.NET.Sdk" /></Project>'
        )
        self.assertEqual((False, True), self.judged(text))

    def test_an_empty_sdk_names_none(self) -> None:
        self.assertEqual((False, False), self.judged('<Project Sdk=""><Import Project="a.props" Sdk="" /></Project>'))

    def test_an_sdk_attribute_on_another_element_is_not_an_import(self) -> None:
        self.assertEqual((False, False), self.judged('<Project><PropertyGroup Sdk="Microsoft.NET.Sdk" /></Project>'))

    def test_an_import_nested_below_the_project_element_is_not_counted(self) -> None:
        text = '<Project><ImportGroup><Import Project="Sdk.props" Sdk="Microsoft.NET.Sdk" /></ImportGroup></Project>'
        self.assertEqual((False, False), self.judged(text))

    def test_a_legacy_project_in_the_msbuild_namespace_is_not_sdk_style(self) -> None:
        text = f'<Project ToolsVersion="15.0" xmlns="{MSBUILD_2003}"><Import Project="Common.targets" /></Project>'
        self.assertEqual((False, False), self.judged(text))

    def test_a_root_that_is_not_a_project_is_not_sdk_style(self) -> None:
        self.assertEqual((False, False), self.judged('<Package Sdk="Microsoft.NET.Sdk"><Sdk Name="x" /></Package>'))

    def test_names_are_compared_with_case(self) -> None:
        self.assertEqual((False, False), self.judged('<project Sdk="Microsoft.NET.Sdk"></project>'))
        self.assertEqual((False, False), self.judged('<Project><sdk Name="Microsoft.NET.Sdk" /></Project>'))

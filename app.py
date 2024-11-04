import ifcopenshell
#import ifcopenshell.api
import ifcopenshell.guid
import ifcopenshell.util.selector
from typing import Union
import streamlit as st
import tempfile
import logging
import os
import zipfile
import pybase64


class Patcher:
    def __init__(self, file: ifcopenshell.file, logger: logging.Logger, stories: list[str], keywords: list[str],
                 ifc_product: str, filter_option: str):
        self.file = file
        self.logger = logger
        self.stories = stories
        self.keywords = keywords
        self.ifc_product = ifc_product
        self.filter_option = filter_option

    def patch(self):
        self.contained_ins: dict[str, set[ifcopenshell.entity_instance]] = {}
        self.aggregates: dict[str, set[ifcopenshell.entity_instance]] = {}
        self.new = ifcopenshell.file(schema=self.file.wrapped_data.schema)
        self.owner_history = None
        self.reuse_identities: dict[int, ifcopenshell.entity_instance] = {}
        for owner_history in self.file.by_type("IfcOwnerHistory"):
            self.owner_history = self.new.add(owner_history)
            break
        self.add_element(self.file.by_type("IfcProject")[0])
        for element in self.filter_elements():
            self.add_element(element)
        self.create_spatial_tree()
        self.file = self.new

    def filter_elements(self):
        elements = ifcopenshell.util.selector.filter_elements(self.file, "IfcProduct")
        filtered_elements = []
        for element in elements:
            if self.filter_option == "IFC Product and Keywords":
                if element.is_a(self.ifc_product) and (
                        not self.keywords or any(keyword.lower() in (element.Name or "").lower() for keyword in self.keywords)):
                    if any(story in [rel.RelatingStructure.Name for rel in getattr(element, "ContainedInStructure", [])]
                           for story in self.stories):
                        filtered_elements.append(element)
            elif self.filter_option == "Keywords Only":
                if any(keyword.lower() in (element.Name or "").lower() for keyword in self.keywords):
                    if any(story in [rel.RelatingStructure.Name for rel in getattr(element, "ContainedInStructure", [])]
                           for story in self.stories):
                        filtered_elements.append(element)
        return filtered_elements

    def add_element(self, element: ifcopenshell.entity_instance) -> None:
        new_element = self.append_asset(element)
        if not new_element:
            return
        self.add_spatial_structures(element, new_element)
        self.add_decomposition_parents(element, new_element)

    def append_asset(self, element: ifcopenshell.entity_instance) -> Union[ifcopenshell.entity_instance, None]:
        try:
            return self.new.by_guid(element.GlobalId)
        except:
            pass
        if element.is_a("IfcProject"):
            return self.new.add(element)
        return ifcopenshell.api.run(
            "project.append_asset", self.new, library=self.file, element=element, reuse_identities=self.reuse_identities
        )

    def add_spatial_structures(
            self, element: ifcopenshell.entity_instance, new_element: ifcopenshell.entity_instance
    ) -> None:
        """element is IfcElement"""
        for rel in getattr(element, "ContainedInStructure", []):
            spatial_element = rel.RelatingStructure
            new_spatial_element = self.append_asset(spatial_element)
            self.contained_ins.setdefault(spatial_element.GlobalId, set()).add(new_element)
            self.add_decomposition_parents(spatial_element, new_spatial_element)

    def add_decomposition_parents(
            self, element: ifcopenshell.entity_instance, new_element: ifcopenshell.entity_instance
    ) -> None:
        """element is IfcObjectDefinition"""
        for rel in element.Decomposes:
            parent = rel.RelatingObject
            new_parent = self.append_asset(parent)
            self.aggregates.setdefault(parent.GlobalId, set()).add(new_element)
            self.add_decomposition_parents(parent, new_parent)
            self.add_spatial_structures(parent, new_parent)

    def create_spatial_tree(self) -> None:
        for relating_structure_guid, related_elements in self.contained_ins.items():
            self.new.createIfcRelContainedInSpatialStructure(
                ifcopenshell.guid.new(),
                self.owner_history,
                None,
                None,
                list(related_elements),
                self.new.by_guid(relating_structure_guid),
            )
        for relating_object_guid, related_objects in self.aggregates.items():
            self.new.createIfcRelAggregates(
                ifcopenshell.guid.new(),
                self.owner_history,
                None,
                None,
                self.new.by_guid(relating_object_guid),
                list(related_objects),
            )


def main():
    st.title("IFC Object Filter")

    uploaded_file = st.file_uploader("Choose an IFC or IFCZIP file", type=["ifc", "ifczip"])

    if uploaded_file is not None:
        input_filename = os.path.splitext(uploaded_file.name)[0]

        with tempfile.NamedTemporaryFile(delete=False) as tmp_file:
            tmp_file.write(uploaded_file.read())
            tmp_file_path = tmp_file.name

        if uploaded_file.name.endswith(".ifczip"):
            with zipfile.ZipFile(tmp_file_path, 'r') as zip_ref:
                zip_ref.extractall(tempfile.gettempdir())
                extracted_files = zip_ref.namelist()
                ifc_files = [f for f in extracted_files if f.endswith(".ifc")]
                if ifc_files:
                    tmp_file_path = os.path.join(tempfile.gettempdir(), ifc_files[0])

        file = ifcopenshell.open(tmp_file_path)

        # Set up logging
        logger = logging.getLogger("IFC Logger")
        logger.setLevel(logging.DEBUG)
        ch = logging.StreamHandler()
        ch.setLevel(logging.DEBUG)
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        ch.setFormatter(formatter)
        logger.addHandler(ch)

        stories_options = ["Keep All Stories"] + [story.Name for story in file.by_type("IfcBuildingStorey")]
        stories = st.multiselect("Select Stories to Keep", options=stories_options)
        if "Keep All Stories" in stories:
            stories = [story.Name for story in file.by_type("IfcBuildingStorey")]

        filter_option = st.selectbox("Choose Filtering Option", options=["IFC Product and Keywords", "Keywords Only"])

        if filter_option == "IFC Product and Keywords":
            # Extract IfcProducts from the input file
            ifc_products = sorted(set(entity.is_a() for entity in file.by_type("IfcProduct")))
            ifc_product = st.selectbox("Select IFC Product to Filter", options=ifc_products)
        else:
            ifc_product = None

        keywords = st.text_input("Enter Keywords to Filter Elements (comma separated)").split(',')

        suffix = f"_stories_{'_'.join(stories)}_product_{ifc_product}_keywords_{'_'.join(keywords)}"
        default_output_filename = f"{input_filename}{suffix}"
        output_filename = st.text_input("Enter Output Filename (optional)", value=default_output_filename)

        patcher = Patcher(file, logger, stories, keywords, ifc_product, filter_option)

        if st.button("Filter IFC Model"):
            patcher.patch()
            if not output_filename:
                output_filename = default_output_filename
            output_filename += ".ifc"
            st.success("IFC model filtered successfully!")
            st.download_button("Download Filtered IFC", data=patcher.file.to_string(), file_name=output_filename)

            # Generate a Blob URL for the filtered IFC file
            ifc_data = patcher.file.to_string()
            b64_ifc_data = pybase64.b64encode(ifc_data.encode()).decode()
            blob_url = f"data:application/octet-stream;base64,{b64_ifc_data}"

            # Display the 3D model viewer
            st.write("## 3D Model Viewer")
            with open("viewer.html", "r") as f:
                html_content = f.read().replace('path_to_your_ifc_file.ifc', blob_url)
            st.components.v1.html(html_content, height=600)

if __name__ == "__main__":
    main()
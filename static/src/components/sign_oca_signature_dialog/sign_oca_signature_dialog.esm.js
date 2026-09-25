/** @odoo-module **/

import {Component, useState} from "@odoo/owl";
import {Dialog} from "@web/core/dialog/dialog";
import {NameAndSignature} from "@web/core/signature/name_and_signature";

/**
 * Diálogo de firma de SDT.
 *
 * Solo ofrece dos formas de firmar: trazarla o cargar una imagen. Se quitó la
 * opción "automática" de Odoo (la que dibujaba el nombre con una tipografía),
 * porque no es la firma de la persona: basta con no pasar `name` y activar
 * `noInputName` para que esa pestaña no se muestre.
 *
 * Si quien firma tiene usuario, puede guardar su trazo y reutilizarlo en los
 * siguientes documentos.
 */
export class SignOcaSignatureDialog extends Component {
    setup() {
        // NameAndSignature inyecta aquí getSignatureImage(), resetSignature()
        // e isSignatureEmpty.
        this.signature = useState({name: "", isSignatureEmpty: true});
        this.state = useState({useSaved: Boolean(this.props.savedSignature)});
    }

    get nameAndSignatureProps() {
        return {
            signature: this.signature,
            noInputName: true,
            mode: "draw",
            fontColor: "DarkBlue",
            onSignatureChange: () => {
                // en cuanto traza algo, deja de usarse la firma guardada
                this.state.useSaved = false;
            },
        };
    }

    get hasSignature() {
        if (this.state.useSaved) {
            return Boolean(this.props.savedSignature);
        }
        return !this.signature.isSignatureEmpty;
    }

    _image() {
        if (this.state.useSaved) {
            return this.props.savedSignature;
        }
        return this.signature.getSignatureImage && this.signature.getSignatureImage();
    }

    onUseSaved() {
        this.state.useSaved = true;
    }

    /** Vuelve al lienzo en blanco. */
    onReset() {
        this.state.useSaved = false;
        if (this.signature.resetSignature) {
            this.signature.resetSignature();
        }
    }

    /** Firma con el trazo actual, sin guardarlo en el perfil. */
    onSign() {
        const image = this._image();
        if (!image) {
            return;
        }
        this.props.uploadSignature({signatureImage: image});
        this.props.close();
    }

    /** Guarda la firma en el usuario y además firma el documento. */
    async onSaveAndSign() {
        const image = this._image();
        if (!image) {
            return;
        }
        await this.props.saveSignature(image);
        this.props.uploadSignature({signatureImage: image});
        this.props.close();
    }
}
SignOcaSignatureDialog.template = "sign_oca.SignatureDialog";
SignOcaSignatureDialog.components = {Dialog, NameAndSignature};
SignOcaSignatureDialog.props = {
    close: Function,
    uploadSignature: Function,
    saveSignature: Function,
    savedSignature: {type: [String, Boolean], optional: true},
    canSaveSignature: {type: Boolean, optional: true},
};
SignOcaSignatureDialog.defaultProps = {
    savedSignature: false,
    canSaveSignature: false,
};
